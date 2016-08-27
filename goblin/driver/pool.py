import asyncio
import collections

import aiohttp

from goblin.driver import connection


class PooledConnection:
    """
    Wrapper for :py:class:`Connection<goblin.driver.connection.Connection>`
    that helps manage tomfoolery associated with connection pooling.

    :param goblin.driver.connection.Connection conn:
    :param goblin.driver.pool.ConnectionPool pool:
    """
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        self._times_acquired = 0

    @property
    def times_acquired(self):
        """
        Readonly property.

        :returns: int
        """
        return self._times_acquired

    def increment_acquired(self):
        """Increment times acquired attribute by 1"""
        self._times_acquired += 1

    def decrement_acquired(self):
        """Decrement times acquired attribute by 1"""
        self._times_acquired -= 1

    async def submit(self,
                     gremlin,
                     *,
                     bindings=None,
                     lang=None,
                     traversal_source=None,
                     session=None):
        """
        **coroutine** Submit a script and bindings to the Gremlin Server

        :param str gremlin: Gremlin script to submit to server.
        :param dict bindings: A mapping of bindings for Gremlin script.
        :param str lang: Language of scripts submitted to the server.
            "gremlin-groovy" by default
        :param dict traversal_source: ``TraversalSource`` objects to different
            variable names in the current request.
        :param str session: Session id (optional). Typically a uuid

        :returns: :py:class:`Response` object
        """
        return await self._conn.submit(gremlin, bindings=bindings, lang=lang,
                                       traversal_source=traversal_source,
                                       session=session)

    async def release_task(self, resp):
        await resp.done.wait()
        self.release()

    def release(self):
        self._pool.release(self)

    async def close(self):
        """???"""
        await self._conn.close()
        self._conn = None
        self._pool = None

    @property
    def closed(self):
        """
        Readonly property.

        :returns: bool
        """
        return self._conn.closed


class ConnectionPool:
    """
    A pool of connections to a Gremlin Server host.

    :param str url: url for host Gremlin Server
    :param asyncio.BaseEventLoop loop:
    :param ssl.SSLContext ssl_context:
    :param str username: Username for database auth
    :param str password: Password for database auth
    :param str lang: Language used to submit scripts (optional)
        `gremlin-groovy` by default
    :param dict traversal_source: Aliases traversal source (optional) `None`
        by default
    :param float response_timeout: (optional) `None` by default
    :param int max_conns: Maximum number of conns to a host
    :param int min_connsd: Minimum number of conns to a host
    :param int max_times_acquired: Maximum number of times a conn can be
        shared by multiple coroutines (clients)
    :param int max_inflight: Maximum number of unprocessed requests at any
        one time on the connection
    """

    def __init__(self, url, loop, *, ssl_context=None, username='',
                 password='', lang='gremlin-groovy', traversal_source=None,
                 response_timeout=None,max_conns=4, min_conns=1,
                 max_times_acquired=16, max_inflight=64):
        self._url = url
        self._loop = loop
        self._ssl_context = ssl_context
        self._username = username
        self._password = password
        self._lang = lang
        self._max_conns = max_conns
        self._min_conns = min_conns
        self._max_times_acquired = max_times_acquired
        self._max_inflight = max_inflight
        self._response_timeout = response_timeout
        self._condition = asyncio.Condition(loop=self._loop)
        self._available = collections.deque()
        self._acquired = collections.deque()
        self._traversal_source = traversal_source

    @property
    def url(self):
        """
        Readonly property.

        :returns: str
        """
        return self._url

    async def init_pool(self):
        """**coroutine** Open minumum number of connections to host"""
        for i in range(self._min_conns):
            conn = await self._get_connection(self._username,
                                              self._password, self._lang,
                                              self._traversal_source,
                                              self._max_inflight,
                                              self._response_timeout)
            self._available.append(conn)

    def release(self, conn):
        """
        Release connection back to pool after use.

        :param PooledConnection conn:
        """
        if conn.closed:
            self._acquired.remove(conn)
        else:
            conn.decrement_acquired()
            if not conn.times_acquired:
                self._acquired.remove(conn)
                self._available.append(conn)
        self._loop.create_task(self._notify())

    async def _notify(self):
        async with self._condition:
            self._condition.notify()

    async def acquire(self, username=None, password=None, lang=None,
                      traversal_source=None, max_inflight=None,
                      response_timeout=None):
        """**coroutine** Acquire a new connection from the pool."""
        username = username or self._username
        password = password or self._password
        traversal_source = traversal_source or self._traversal_source
        response_timeout = response_timeout or self._response_timeout
        max_inflight = max_inflight or self._max_inflight
        lang = lang or self._lang
        async with self._condition:
            while True:
                while self._available:
                    conn = self._available.popleft()
                    if not conn.closed:
                        conn.increment_acquired()
                        self._acquired.append(conn)
                        return conn
                if len(self._acquired) < self._max_conns:
                    conn = await self._get_connection(username, password, lang,
                                                      traversal_source,
                                                      max_inflight,
                                                      response_timeout)
                    conn.increment_acquired()
                    self._acquired.append(conn)
                    return conn
                else:
                    for x in range(len(self._acquired)):
                        conn = self._acquired.popleft()
                        if conn.times_acquired < self._max_times_acquired:
                            conn.increment_acquired()
                            self._acquired.append(conn)
                            return conn
                        self._acquired.append(conn)
                    else:
                        await self._condition.wait()

    async def close(self):
        """**coroutine** Close connection pool."""
        waiters = []
        while self._available:
            conn = self._available.popleft()
            waiters.append(conn.close())
        while self._acquired:
            conn = self._acquired.popleft()
            waiters.append(conn.close())
        await asyncio.gather(*waiters)

    async def _get_connection(self, username, password, lang,
                              traversal_source, max_inflight,
                              response_timeout):
        conn = await connection.Connection.open(
            self._url, self._loop, ssl_context=self._ssl_context,
            username=username, password=password, lang=lang,
            max_inflight=max_inflight, traversal_source=traversal_source,
            response_timeout=response_timeout)
        conn = PooledConnection(conn, self)
        return conn
