"""Query API and helpers"""
import asyncio
import functools
import logging

from goblin import mapper
from goblin.driver import connection, graph
from goblin.gremlin_python import process


logger = logging.getLogger(__name__)


class TraversalResponse:

    def __init__(self, response_queue):
        self._queue = response_queue
        self._done = False

    async def __aiter__(self):
        return self

    async def __anext__(self):
        if self._done:
            return
        msg = await self._queue.get()
        if msg:
            return msg
        else:
            self._done = True
            raise StopAsyncIteration


# This is all a hack until we figure out GLV integration...
class GoblinTraversal(graph.AsyncGraphTraversal):

    async def all(self):
        return await self.next()

    async def one(self):
        # Idk really know how one will work
        async for element in await self.all():
            return element


class Traversal(connection.AbstractConnection):
    """Wrapper for AsyncRemoteGraph that functions as a remote connection.
       Used to generate/submit traversals."""
    def __init__(self, session, translator, loop, *, element=None,
                 element_class=None):
        self._session = session
        self._translator = translator
        self._loop = loop
        self._element = element
        self._element_class = element_class
        self._graph = graph.AsyncRemoteGraph(self._translator,
                                             self,  # Traversal implements RC
                                             graph_traversal=GoblinTraversal)

    @property
    def graph(self):
        return self._graph

    @property
    def session(self):
        return self._session

    def traversal(self):
        if self._element_class:
            label = self._element_class.__mapping__.label
            traversal = self._graph.traversal()
            if self._element_class.__type__ == 'vertex':
                traversal = traversal.V()
            if self._element_class.__type__ == 'edge':
                traversal = traversal.E()
            traversal = traversal.hasLabel(label)
        else:
            traversal = self.graph.traversal()
        return traversal

    async def submit(self,
                    gremlin,
                    *,
                    bindings=None,
                    lang='gremlin-groovy'):
        """Get all results generated by query"""
        async_iter = await self.session.submit(
            gremlin, bindings=bindings, lang=lang)
        response_queue = asyncio.Queue(loop=self._loop)
        self._loop.create_task(
            self._receive(async_iter, response_queue))
        return TraversalResponse(response_queue)

    async def _receive(self, async_iter, response_queue):
        async for msg in async_iter:
            results = msg.data
            if results:
                for result in results:
                    current = self.session.current.get(result['id'], None)
                    if not current:
                        if self._element or self._element_class:
                            current = self._element or self._element_class()
                        else:
                            # build generic element here
                            pass
                    element = current.__mapping__.mapper_func(
                        result, current)
                    response_queue.put_nowait(element)
        response_queue.put_nowait(None)


class TraversalFactory:

    def __init__(self, session, translator, loop):
        self._session = session
        self._translator = translator
        self._loop = loop
        self._binding = 0

    def traversal(self, *, element=None, element_class=None):
        return Traversal(self._session,
                         self._translator,
                         self._loop,
                         element=element,
                         element_class=element_class)

    async def remove_vertex(self, element):
        traversal = self.traversal(element=element)
        return await traversal.graph.traversal().V(element.id).drop().one()

    async def remove_edge(self, element):
        traversal = self.traversal(element=element)
        return await traversal.graph.traversal().E(element.id).drop().one()

    async def get_vertex_by_id(self, element):
        traversal = self.traversal(element=element)
        return await traversal.graph.traversal().V(element.id).one()

    async def get_edge_by_id(self, element):
        traversal = self.traversal(element=element)
        return await traversal.graph.traversal().E(element.id).one()

    async def add_vertex(self, element):
        props = mapper.map_props_to_db(element, element.__mapping__)
        traversal = self.traversal(element=element)
        traversal = traversal.graph.traversal().addV(element.__mapping__.label)
        return await self._add_properties(traversal, props).one()

    async def add_edge(self, element):
        props = mapper.map_props_to_db(element, element.__mapping__)
        base_traversal = self.traversal(element=element)
        traversal = base_traversal.graph.traversal().V(element.source.id)
        traversal = traversal.addE(element.__mapping__._label)
        traversal = traversal.to(
            base_traversal.graph.traversal().V(element.target.id))
        return await self._add_properties(traversal, props).one()

    async def update_vertex(self, element):
        props = mapper.map_props_to_db(element, element.__mapping__)
        traversal = self.traversal(element=element)
        traversal = traversal.graph.traversal().V(element.id)
        return await self._add_properties(traversal, props).one()

    async def update_edge(self, element):
        props = mapper.map_props_to_db(element, element.__mapping__)
        traversal = self.traversal(element=element)
        traversal = traversal.graph.traversal().E(element.id)
        return await self._add_properties(traversal, props).one()

    def _add_properties(self, traversal, props):
        for k, v in props:
            if v:
                traversal = traversal.property(
                    ('k' + str(self._binding), k),
                    ('v' + str(self._binding), v))
                self._binding += 1
        self._binding = 0
        return traversal