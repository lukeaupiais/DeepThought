"""Port of the original Neo4jConnectionManager without application settings.

Configuration belongs to the embedding caller; the optional driver is imported
only when connecting. Driver-managed retries replace application-wide settings.
"""


class Neo4jConnectionManager:
    def __init__(self, uri, user, password, *, database="neo4j", timeout_seconds=10):
        self.uri = uri
        self.user = user
        self.password = password
        self.database = database
        self.timeout_seconds = timeout_seconds
        self.driver = None

    async def connect(self):
        if self.driver is not None:
            return self.driver
        try:
            from neo4j import AsyncGraphDatabase
        except ImportError as exc:
            raise ImportError("Install deepthought-analysis-engine[neo4j]") from exc
        driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password),
            connection_timeout=self.timeout_seconds,
            max_transaction_retry_time=self.timeout_seconds,
        )
        try:
            await driver.verify_connectivity()
        except BaseException:
            await driver.close()
            raise
        self.driver = driver
        return driver

    async def close(self):
        if self.driver is not None:
            driver, self.driver = self.driver, None
            await driver.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()
