"""Use the exact tokenizer embedded in the running Nemotron GGUF."""
import json
from types import SimpleNamespace
from urllib.request import Request, urlopen


class RemoteTokenizer:
    def __init__(self, api_base):
        self.api_base = api_base.rstrip("/")

    def request(self, endpoint, payload):
        request = Request(self.api_base + endpoint, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    def encode(self, text):
        result = self.request("/tokenize", {"content": text, "add_special": False})
        return SimpleNamespace(ids=result["tokens"])

    def decode(self, token_ids, skip_special_tokens=True):
        return self.request("/detokenize", {"tokens": token_ids})["content"]
