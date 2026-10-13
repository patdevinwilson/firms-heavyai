"""Restore HeavyIQ's three catalog helpers over pyheavydb 10's Thrift client."""


def ensure_connection_api():
    from heavydb import Connection
    from heavydb._parsers import _extract_column_details

    if not hasattr(Connection, "get_table_details"):
        Connection.get_table_details = lambda self, table: self._client.get_table_details(self._session, table)
    if not hasattr(Connection, "get_column_details"):
        Connection.get_column_details = lambda self, table: _extract_column_details(self.get_table_details(table).row_desc)
    if not hasattr(Connection, "get_tables"):
        Connection.get_tables = lambda self: self._client.get_tables(self._session)
