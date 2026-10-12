"""Generate portable CloudFormation JSON; no account-specific resources."""
import json
from pathlib import Path


def ref(name):
    return {"Ref": name}


def sub(value):
    return {"Fn::Sub": value}


def attr(name, attribute):
    return {"Fn::GetAtt": [name, attribute]}


def template(existing_network=False):
    resources = {}
    def add(name, kind, properties, **extras):
        resources[name] = {"Type": "AWS::" + kind, "Properties": properties, **extras}
    add("Archive", "S3::Bucket", {
        "BucketEncryption": {"ServerSideEncryptionConfiguration": [
            {"ServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
        "VersioningConfiguration": {"Status": "Enabled"},
        "LifecycleConfiguration": {"Rules": [{"Id": "raw-retention", "Status": "Enabled",
             "Prefix": "raw/", "ExpirationInDays": 365,
             "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}]},
    }, DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    add("BucketPolicy", "S3::BucketPolicy", {"Bucket": ref("Archive"), "PolicyDocument": {
        "Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Principal": "*",
        "Action": "s3:*", "Resource": [attr("Archive", "Arn"), sub("${Archive.Arn}/*")],
        "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}})
    add("Secret", "SecretsManager::Secret", {
        "Description": "FIRMS and HEAVY.AI runtime credentials; populated by deploy CLI"},
        DeletionPolicy="Retain", UpdateReplacePolicy="Retain")
    add("VPC", "EC2::VPC", {"CidrBlock": "10.86.0.0/16", "EnableDnsSupport": True,
                            "EnableDnsHostnames": True})
    add("InternetGateway", "EC2::InternetGateway", {})
    add("GatewayAttachment", "EC2::VPCGatewayAttachment", {
        "VpcId": ref("VPC"), "InternetGatewayId": ref("InternetGateway")})
    add("Subnet", "EC2::Subnet", {"VpcId": ref("VPC"), "CidrBlock": "10.86.1.0/24",
                                  "MapPublicIpOnLaunch": True})
    add("RouteTable", "EC2::RouteTable", {"VpcId": ref("VPC")})
    add("InternetRoute", "EC2::Route", {"RouteTableId": ref("RouteTable"),
         "DestinationCidrBlock": "0.0.0.0/0", "GatewayId": ref("InternetGateway")},
         DependsOn="GatewayAttachment")
    add("RouteAssociation", "EC2::SubnetRouteTableAssociation", {
        "SubnetId": ref("Subnet"), "RouteTableId": ref("RouteTable")})
    add("SecurityGroup", "EC2::SecurityGroup", {"VpcId": ref("VPC"),
         "GroupDescription": "SSM-only access; no inbound ports", "SecurityGroupIngress": [],
         "SecurityGroupEgress": [{"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}]})
    add("Role", "IAM::Role", {"AssumeRolePolicyDocument": {"Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"},
                       "Action": "sts:AssumeRole"}]},
        "ManagedPolicyArns": [sub("arn:${AWS::Partition}:iam::aws:policy/AmazonSSMManagedInstanceCore")],
        "Policies": [{"PolicyName": "Runtime", "PolicyDocument": {
            "Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject"],
                 "Resource": sub("${Archive.Arn}/*")},
                {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": attr("Archive", "Arn")},
                {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": ref("Secret")}
            ]}}]})
    add("Profile", "IAM::InstanceProfile", {"Roles": [ref("Role")]})
    userdata = (Path(__file__).with_name("userdata.sh")).read_text()
    add("Instance", "EC2::Instance", {
        "ImageId": ref("AmiId"), "InstanceType": ref("InstanceType"),
        "IamInstanceProfile": ref("Profile"),
        "NetworkInterfaces": [{"DeviceIndex": "0", "AssociatePublicIpAddress": True,
             "SubnetId": ref("Subnet"), "GroupSet": [ref("SecurityGroup")]}],
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 2},
        "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {
            "VolumeSize": ref("DiskGb"), "VolumeType": "gp3", "Encrypted": True,
            "DeleteOnTermination": True}}],
        "Tags": [{"Key": "Name", "Value": ref("AWS::StackName")},
                 {"Key": "Project", "Value": "firms-heavyai"}],
        "UserData": {"Fn::Base64": userdata}}, DependsOn=["InternetRoute", "RouteAssociation"])
    result = {"AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Global FIRMS activity demo with source-built HEAVY.AI and local HeavyIQ model",
        "Parameters": {
            "AmiId": {"Type": "AWS::EC2::Image::Id"},
            "InstanceType": {"Type": "String", "Default": "g5.4xlarge",
                             "AllowedValues": ["g5.4xlarge"]},
            "DiskGb": {"Type": "Number", "Default": 500, "MinValue": 100, "MaxValue": 2000}},
        "Resources": resources,
        "Outputs": {"ArchiveBucket": {"Value": ref("Archive")},
                    "SecretArn": {"Value": ref("Secret")},
                    "InstanceId": {"Value": ref("Instance")}}}
    if existing_network:
        for name in ("VPC", "InternetGateway", "GatewayAttachment", "Subnet", "RouteTable",
                     "InternetRoute", "RouteAssociation"):
            del resources[name]
        result["Parameters"].update({
            "ExistingVpcId": {"Type": "AWS::EC2::VPC::Id"},
            "ExistingSubnetId": {"Type": "AWS::EC2::Subnet::Id"}})
        resources["SecurityGroup"]["Properties"]["VpcId"] = ref("ExistingVpcId")
        resources["Instance"]["Properties"]["NetworkInterfaces"][0]["SubnetId"] = ref("ExistingSubnetId")
        resources["Instance"].pop("DependsOn")
    return result


if __name__ == "__main__":
    print(json.dumps(template(), indent=2))
