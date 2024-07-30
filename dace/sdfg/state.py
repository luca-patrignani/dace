# Copyright 2019-2024 ETH Zurich and the DaCe authors. All rights reserved.
""" Contains classes of a single SDFG state and dataflow subgraphs. """

import ast
import abc
import collections
import copy
import inspect
import itertools
import warnings
from typing import (TYPE_CHECKING, Any, AnyStr, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union,
                    overload)

import dace
import dace.serialize
from dace import data as dt
from dace import dtypes
from dace import memlet as mm
from dace import serialize
from dace import subsets as sbs
from dace import symbolic
from dace.properties import (CodeBlock, DebugInfoProperty, DictProperty, EnumProperty, Property, SubsetProperty, SymbolicProperty,
                             CodeProperty, make_properties)
from dace.sdfg import nodes as nd
from dace.sdfg.graph import MultiConnectorEdge, OrderedMultiDiConnectorGraph, SubgraphView, OrderedDiGraph, Edge
from dace.sdfg.propagation import propagate_memlet
from dace.sdfg.validation import validate_state
from dace.subsets import Range, Subset

if TYPE_CHECKING:
    import dace.sdfg.scope
    from dace.sdfg import SDFG

NodeT = Union[nd.Node, 'ControlFlowBlock']
EdgeT = Union[MultiConnectorEdge[mm.Memlet], Edge['dace.sdfg.InterstateEdge']]
GraphT = Union['ControlFlowRegion', 'SDFGState']


def _getdebuginfo(old_dinfo=None) -> dtypes.DebugInfo:
    """ Returns a DebugInfo object for the position that called this function.

        :param old_dinfo: Another DebugInfo object that will override the
                          return value of this function
        :return: DebugInfo containing line number and calling file.
    """
    if old_dinfo is not None:
        return old_dinfo

    caller = inspect.getframeinfo(inspect.stack()[2][0], context=0)
    return dtypes.DebugInfo(caller.lineno, 0, caller.lineno, 0, caller.filename)


def _make_iterators(ndrange):
    # Input can either be a dictionary or a list of pairs
    if isinstance(ndrange, list):
        params = [k for k, _ in ndrange]
        ndrange = {k: v for k, v in ndrange}
    else:
        params = list(ndrange.keys())

    # Parse each dimension separately
    ranges = []
    for p in params:
        prange: Union[str, sbs.Subset, Tuple[symbolic.SymbolicType]] = ndrange[p]
        if isinstance(prange, sbs.Subset):
            rng = prange.ndrange()[0]
        elif isinstance(prange, tuple):
            rng = prange
        else:
            rng = SubsetProperty.from_string(prange)[0]
        ranges.append(rng)
    map_range = sbs.Range(ranges)

    return params, map_range


class BlockGraphView(object):
    """
    Read-only view interface of an SDFG control flow block, containing methods for memlet tracking, traversal, subgraph
    creation, queries, and replacements. ``ControlFlowBlock`` and ``StateSubgraphView`` inherit from this class to share
    methods.
    """

    ###################################################################
    # Typing overrides

    @overload
    def nodes(self) -> List[NodeT]:
        ...

    @overload
    def edges(self) -> List[EdgeT]:
        ...

    @overload
    def in_degree(self, node: NodeT) -> int:
        ...

    @overload
    def out_degree(self, node: NodeT) -> int:
        ...

    @property
    def sdfg(self) -> 'SDFG':
        ...

    ###################################################################
    # Traversal methods

    @abc.abstractmethod
    def all_nodes_recursive(
        self,
        predicate: Optional[Callable[[NodeT, GraphT], bool]] = None) -> Iterator[Tuple[NodeT, GraphT]]:
        """
        Iterate over all nodes in this graph or subgraph.
        This includes control flow blocks, nodes in those blocks, and recursive control flow blocks and nodes within
        nested SDFGs. It returns tuples of the form (node, parent), where the node is either a dataflow node, in which
        case the parent is an SDFG state, or a control flow block, in which case the parent is a control flow graph
        (i.e., an SDFG or a scope block).

        :param predicate: An optional predicate function that decides on whether the traversal should recurse or not.
        If the predicate returns False, traversal is not recursed any further into the graph found under NodeT for
        a given [NodeT, GraphT] pair.
        """
        return []

    @abc.abstractmethod
    def all_edges_recursive(self) -> Iterator[Tuple[EdgeT, GraphT]]:
        """
        Iterate over all edges in this graph or subgraph.
        This includes dataflow edges, inter-state edges, and recursive edges within nested SDFGs. It returns tuples of
        the form (edge, parent), where the edge is either a dataflow edge, in which case the parent is an SDFG state, or
        an inter-stte edge, in which case the parent is a control flow graph (i.e., an SDFG or a scope block).
        """
        return []

    @abc.abstractmethod
    def data_nodes(self) -> List[nd.AccessNode]:
        """
        Returns all data nodes (i.e., AccessNodes, arrays) present in this graph or subgraph.
        Note: This does not recurse into nested SDFGs.
        """
        return []

    @abc.abstractmethod
    def entry_node(self, node: nd.Node) -> Optional[nd.EntryNode]:
        """ Returns the entry node that wraps the current node, or None if it is top-level in a state. """
        return None

    @abc.abstractmethod
    def exit_node(self, entry_node: nd.EntryNode) -> Optional[nd.ExitNode]:
        """ Returns the exit node leaving the context opened by the given entry node. """
        raise None

    ###################################################################
    # Memlet-tracking methods

    @abc.abstractmethod
    def memlet_path(self, edge: MultiConnectorEdge[mm.Memlet]) -> List[MultiConnectorEdge[mm.Memlet]]:
        """
        Given one edge, returns a list of edges representing a path between its source and sink nodes.
        Used for memlet tracking.

        :note: Behavior is undefined when there is more than one path involving this edge.
        :param edge: An edge within a state (memlet).
        :return: A list of edges from a source node to a destination node.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def memlet_tree(self, edge: MultiConnectorEdge) -> mm.MemletTree:
        """
        Given one edge, returns a tree of edges between its node source(s) and sink(s).
        Used for memlet tracking.

        :param edge: An edge within a state (memlet).
        :return: A tree of edges whose root is the source/sink node (depending on direction) and associated children
                 edges.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def in_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        """
        Returns a generator over edges entering the given connector of the given node.

        :param node: Destination node of edges.
        :param connector: Destination connector of edges.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def out_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        """
        Returns a generator over edges exiting the given connector of the given node.

        :param node: Source node of edges.
        :param connector: Source connector of edges.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        """
        Returns a generator over edges entering or exiting the given connector of the given node.

        :param node: Source/destination node of edges.
        :param connector: Source/destination connector of edges.
        """
        raise NotImplementedError()

    ###################################################################
    # Query, subgraph, and replacement methods

    @abc.abstractmethod
    def used_symbols(self, all_symbols: bool, keep_defined_in_mapping: bool = False) -> Set[str]:
        """
        Returns a set of symbol names that are used in the graph.

        :param all_symbols: If False, only returns symbols that are needed as arguments (only used in generated code).
        :param keep_defined_in_mapping: If True, symbols defined in inter-state edges that are in the symbol mapping
                                        will be removed from the set of defined symbols.
        """
        return set()

    @property
    def free_symbols(self) -> Set[str]:
        """
        Returns a set of symbol names that are used, but not defined, in this graph view.
        In the case of an SDFG, this property is used to determine the symbolic parameters of the SDFG and
        verify that ``SDFG.symbols`` is complete.

        :note: Assumes that the graph is valid (i.e., without undefined or overlapping symbols).
        """
        return self.used_symbols(all_symbols=True)

    @abc.abstractmethod
    def read_and_write_sets(self) -> Tuple[Set[AnyStr], Set[AnyStr]]:
        """
        Determines what data is read and written in this graph.
        Does not include reads to subsets of containers that have previously been written within the same state.
        
        :return: A two-tuple of sets of things denoting ({data read}, {data written}).
        """
        return set(), set()

    @abc.abstractmethod
    def unordered_arglist(self,
                          defined_syms=None,
                          shared_transients=None) -> Tuple[Dict[str, dt.Data], Dict[str, dt.Data]]:
        return {}, {}

    def arglist(self, defined_syms=None, shared_transients=None) -> Dict[str, dt.Data]:
        """
        Returns an ordered dictionary of arguments (names and types) required to invoke this subgraph.

        The arguments differ from SDFG.arglist, but follow the same order,
        namely: <sorted data arguments>, <sorted scalar arguments>.

        Data arguments contain:
            * All used non-transient data containers in the subgraph
            * All used transient data containers that were allocated outside.
              This includes data from memlets, transients shared across multiple states, and transients that could not
              be allocated within the subgraph (due to their ``AllocationLifetime`` or according to the
              ``dtypes.can_allocate`` function).

        Scalar arguments contain:
            * Free symbols in this state/subgraph.
            * All transient and non-transient scalar data containers used in this subgraph.

        This structure will create a sorted list of pointers followed by a sorted list of PoDs and structs.

        :return: An ordered dictionary of (name, data descriptor type) of all the arguments, sorted as defined here.
        """
        data_args, scalar_args = self.unordered_arglist(defined_syms, shared_transients)

        # Fill up ordered dictionary
        result = collections.OrderedDict()
        for k, v in itertools.chain(sorted(data_args.items()), sorted(scalar_args.items())):
            result[k] = v

        return result

    def signature_arglist(self, with_types=True, for_call=False):
        """ Returns a list of arguments necessary to call this state or subgraph, formatted as a list of C definitions.

            :param with_types: If True, includes argument types in the result.
            :param for_call: If True, returns arguments that can be used when calling the SDFG.
            :return: A list of strings. For example: `['float *A', 'int b']`.
        """
        return [v.as_arg(name=k, with_types=with_types, for_call=for_call) for k, v in self.arglist().items()]

    @abc.abstractmethod
    def top_level_transients(self) -> Set[str]:
        """Iterate over top-level transients of this graph."""
        return set()

    @abc.abstractmethod
    def all_transients(self) -> List[str]:
        """Iterate over all transients in this graph."""
        return []

    @abc.abstractmethod
    def replace(self, name: str, new_name: str):
        """
        Finds and replaces all occurrences of a symbol or array in this graph.

        :param name: Name to find.
        :param new_name: Name to replace.
        """
        pass

    @abc.abstractmethod
    def replace_dict(self,
                     repl: Dict[str, str],
                     symrepl: Optional[Dict[symbolic.SymbolicType, symbolic.SymbolicType]] = None):
        """
        Finds and replaces all occurrences of a set of symbols or arrays in this graph.

        :param repl: Mapping from names to replacements.
        :param symrepl: Optional symbolic version of ``repl``.
        """
        pass


@make_properties
class DataflowGraphView(BlockGraphView, abc.ABC):

    def __init__(self, *args, **kwargs):
        self._clear_scopedict_cache()

    ###################################################################
    # Typing overrides

    @overload
    def nodes(self) -> List[nd.Node]:
        ...

    @overload
    def edges(self) -> List[MultiConnectorEdge[mm.Memlet]]:
        ...

    ###################################################################
    # Traversal methods

    def all_nodes_recursive(self, predicate = None) -> Iterator[Tuple[NodeT, GraphT]]:
        for node in self.nodes():
            yield node, self
            if isinstance(node, nd.NestedSDFG):
                if predicate is None or predicate(node, self):
                    yield from node.sdfg.all_nodes_recursive()

    def all_edges_recursive(self) -> Iterator[Tuple[EdgeT, GraphT]]:
        for e in self.edges():
            yield e, self
        for node in self.nodes():
            if isinstance(node, nd.NestedSDFG):
                yield from node.sdfg.all_edges_recursive()

    def data_nodes(self) -> List[nd.AccessNode]:
        """ Returns all data_nodes (arrays) present in this state. """
        return [n for n in self.nodes() if isinstance(n, nd.AccessNode)]

    def entry_node(self, node: nd.Node) -> Optional[nd.EntryNode]:
        """ Returns the entry node that wraps the current node, or None if
            it is top-level in a state. """
        return self.scope_dict()[node]

    def exit_node(self, entry_node: nd.EntryNode) -> Optional[nd.ExitNode]:
        """ Returns the exit node leaving the context opened by
            the given entry node. """
        node_to_children = self.scope_children()
        return next(v for v in node_to_children[entry_node] if isinstance(v, nd.ExitNode))

    ###################################################################
    # Memlet-tracking methods

    def memlet_path(self, edge: MultiConnectorEdge[mm.Memlet]) -> List[MultiConnectorEdge[mm.Memlet]]:
        """ Given one edge, returns a list of edges representing a path
            between its source and sink nodes. Used for memlet tracking.

            :note: Behavior is undefined when there is more than one path
                   involving this edge.
            :param edge: An edge within this state.
            :return: A list of edges from a source node to a destination node.
            """
        result = [edge]

        # Obtain the full state (to work with paths that trace beyond a scope)
        state = self._graph

        # If empty memlet, return itself as the path
        if (edge.src_conn is None and edge.dst_conn is None and edge.data.is_empty()):
            return result

        # Prepend incoming edges until reaching the source node
        curedge = edge
        visited = set()
        while not isinstance(curedge.src, (nd.CodeNode, nd.AccessNode)):
            visited.add(curedge)
            # Trace through scopes using OUT_# -> IN_#
            if isinstance(curedge.src, (nd.EntryNode, nd.ExitNode)):
                if curedge.src_conn is None:
                    raise ValueError("Source connector cannot be None for {}".format(curedge.src))
                assert curedge.src_conn.startswith("OUT_")
                next_edge = next(e for e in state.in_edges(curedge.src) if e.dst_conn == "IN_" + curedge.src_conn[4:])
                result.insert(0, next_edge)
                curedge = next_edge
                if curedge in visited:
                    raise ValueError('Cycle encountered while reading memlet path')

        # Append outgoing edges until reaching the sink node
        curedge = edge
        visited.clear()
        while not isinstance(curedge.dst, (nd.CodeNode, nd.AccessNode)):
            visited.add(curedge)
            # Trace through scope entry using IN_# -> OUT_#
            if isinstance(curedge.dst, (nd.EntryNode, nd.ExitNode)):
                if curedge.dst_conn is None:
                    raise ValueError("Destination connector cannot be None for {}".format(curedge.dst))
                if not curedge.dst_conn.startswith("IN_"):  # Map variable
                    break
                next_edge = next(e for e in state.out_edges(curedge.dst) if e.src_conn == "OUT_" + curedge.dst_conn[3:])
                result.append(next_edge)
                curedge = next_edge
                if curedge in visited:
                    raise ValueError('Cycle encountered while reading memlet path')

        return result

    def memlet_tree(self, edge: MultiConnectorEdge) -> mm.MemletTree:
        propagate_forward = False
        propagate_backward = False
        if ((isinstance(edge.src, nd.EntryNode) and edge.src_conn is not None) or
            (isinstance(edge.dst, nd.EntryNode) and edge.dst_conn is not None and edge.dst_conn.startswith('IN_'))):
            propagate_forward = True
        if ((isinstance(edge.src, nd.ExitNode) and edge.src_conn is not None)
                or (isinstance(edge.dst, nd.ExitNode) and edge.dst_conn is not None)):
            propagate_backward = True

        # If either both are False (no scopes involved) or both are True
        # (invalid SDFG), we return only the current edge as a degenerate tree
        if propagate_forward == propagate_backward:
            return mm.MemletTree(edge)

        # Obtain the full state (to work with paths that trace beyond a scope)
        state = self._graph

        # Find tree root
        curedge = edge
        visited = set()
        if propagate_forward:
            while (isinstance(curedge.src, nd.EntryNode) and curedge.src_conn is not None):
                visited.add(curedge)
                assert curedge.src_conn.startswith('OUT_')
                cname = curedge.src_conn[4:]
                curedge = next(e for e in state.in_edges(curedge.src) if e.dst_conn == 'IN_%s' % cname)
                if curedge in visited:
                    raise ValueError('Cycle encountered while reading memlet path')
        elif propagate_backward:
            while (isinstance(curedge.dst, nd.ExitNode) and curedge.dst_conn is not None):
                visited.add(curedge)
                assert curedge.dst_conn.startswith('IN_')
                cname = curedge.dst_conn[3:]
                curedge = next(e for e in state.out_edges(curedge.dst) if e.src_conn == 'OUT_%s' % cname)
                if curedge in visited:
                    raise ValueError('Cycle encountered while reading memlet path')
        tree_root = mm.MemletTree(curedge, downwards=propagate_forward)

        # Collect children (recursively)
        def add_children(treenode):
            if propagate_forward:
                if not (isinstance(treenode.edge.dst, nd.EntryNode) and treenode.edge.dst_conn
                        and treenode.edge.dst_conn.startswith('IN_')):
                    return
                conn = treenode.edge.dst_conn[3:]
                treenode.children = [
                    mm.MemletTree(e, downwards=True, parent=treenode) for e in state.out_edges(treenode.edge.dst)
                    if e.src_conn == 'OUT_%s' % conn
                ]
            elif propagate_backward:
                if (not isinstance(treenode.edge.src, nd.ExitNode) or treenode.edge.src_conn is None):
                    return
                conn = treenode.edge.src_conn[4:]
                treenode.children = [
                    mm.MemletTree(e, downwards=False, parent=treenode) for e in state.in_edges(treenode.edge.src)
                    if e.dst_conn == 'IN_%s' % conn
                ]

            for child in treenode.children:
                add_children(child)

        # Start from root node (obtained from above parent traversal)
        add_children(tree_root)

        # Find edge in tree
        def traverse(node):
            if node.edge == edge:
                return node
            for child in node.children:
                res = traverse(child)
                if res is not None:
                    return res
            return None

        # Return node that corresponds to current edge
        return traverse(tree_root)

    def in_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        return (e for e in self.in_edges(node) if e.dst_conn == connector)

    def out_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        return (e for e in self.out_edges(node) if e.src_conn == connector)

    def edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        return itertools.chain(self.in_edges_by_connector(node, connector),
                               self.out_edges_by_connector(node, connector))

    ###################################################################
    # Scope-related methods

    def _clear_scopedict_cache(self):
        """
        Clears the cached results for the scope_dict function.
        For use when the graph mutates (e.g., new edges/nodes, deletions).
        """
        self._scope_dict_toparent_cached = None
        self._scope_dict_tochildren_cached = None
        self._scope_tree_cached = None
        self._scope_leaves_cached = None

    def scope_tree(self) -> 'dace.sdfg.scope.ScopeTree':
        from dace.sdfg.scope import ScopeTree

        if (hasattr(self, '_scope_tree_cached') and self._scope_tree_cached is not None):
            return copy.copy(self._scope_tree_cached)

        sdp = self.scope_dict()
        sdc = self.scope_children()

        result = {}

        # Get scopes
        for node, scopenodes in sdc.items():
            if node is None:
                exit_node = None
            else:
                exit_node = next(v for v in scopenodes if isinstance(v, nd.ExitNode))
            scope = ScopeTree(node, exit_node)
            result[node] = scope

        # Scope parents and children
        for node, scope in result.items():
            if node is not None:
                scope.parent = result[sdp[node]]
            scope.children = [result[n] for n in sdc[node] if isinstance(n, nd.EntryNode)]

        self._scope_tree_cached = result

        return copy.copy(self._scope_tree_cached)

    def scope_leaves(self) -> List['dace.sdfg.scope.ScopeTree']:
        if (hasattr(self, '_scope_leaves_cached') and self._scope_leaves_cached is not None):
            return copy.copy(self._scope_leaves_cached)
        st = self.scope_tree()
        self._scope_leaves_cached = [scope for scope in st.values() if len(scope.children) == 0]
        return copy.copy(self._scope_leaves_cached)

    def scope_dict(self, return_ids: bool = False, validate: bool = True) -> Dict[nd.Node, Union['SDFGState', nd.Node]]:
        from dace.sdfg.scope import _scope_dict_inner, _scope_dict_to_ids
        result = None
        result = copy.copy(self._scope_dict_toparent_cached)

        if result is None:
            result = {}
            node_queue = collections.deque(self.source_nodes())
            eq = _scope_dict_inner(self, node_queue, None, False, result)

            # Sanity checks
            if validate and len(eq) != 0:
                cycles = list(self.find_cycles())
                if cycles:
                    raise ValueError('Found cycles in state %s: %s' % (self.label, cycles))
                raise RuntimeError("Leftover nodes in queue: {}".format(eq))

            if validate and len(result) != self.number_of_nodes():
                cycles = list(self.find_cycles())
                if cycles:
                    raise ValueError('Found cycles in state %s: %s' % (self.label, cycles))
                leftover_nodes = set(self.nodes()) - result.keys()
                raise RuntimeError("Some nodes were not processed: {}".format(leftover_nodes))

            # Cache result
            self._scope_dict_toparent_cached = result
            result = copy.copy(result)

        if return_ids:
            return _scope_dict_to_ids(self, result)
        return result

    def scope_children(self,
                       return_ids: bool = False,
                       validate: bool = True) -> Dict[Union[nd.Node, 'SDFGState'], List[nd.Node]]:
        from dace.sdfg.scope import _scope_dict_inner, _scope_dict_to_ids
        result = None
        if self._scope_dict_tochildren_cached is not None:
            result = copy.copy(self._scope_dict_tochildren_cached)

        if result is None:
            result = {}
            node_queue = collections.deque(self.source_nodes())
            eq = _scope_dict_inner(self, node_queue, None, True, result)

            # Sanity checks
            if validate and len(eq) != 0:
                cycles = self.find_cycles()
                if cycles:
                    raise ValueError('Found cycles in state %s: %s' % (self.label, list(cycles)))
                raise RuntimeError("Leftover nodes in queue: {}".format(eq))

            entry_nodes = set(n for n in self.nodes() if isinstance(n, nd.EntryNode)) | {None}
            if (validate and len(result) != len(entry_nodes)):
                cycles = self.find_cycles()
                if cycles:
                    raise ValueError('Found cycles in state %s: %s' % (self.label, list(cycles)))
                raise RuntimeError("Some nodes were not processed: {}".format(entry_nodes - result.keys()))

            # Cache result
            self._scope_dict_tochildren_cached = result
            result = copy.copy(result)

        if return_ids:
            return _scope_dict_to_ids(self, result)
        return result

    ###################################################################
    # Query, subgraph, and replacement methods

    def is_leaf_memlet(self, e):
        if isinstance(e.src, nd.ExitNode) and e.src_conn and e.src_conn.startswith('OUT_'):
            return False
        if isinstance(e.dst, nd.EntryNode) and e.dst_conn and e.dst_conn.startswith('IN_'):
            return False
        return True

    def used_symbols(self, all_symbols: bool, keep_defined_in_mapping: bool = False) -> Set[str]:
        state = self.graph if isinstance(self, SubgraphView) else self
        sdfg = state.sdfg
        new_symbols = set()
        freesyms = set()

        # Free symbols from nodes
        for n in self.nodes():
            if isinstance(n, nd.EntryNode):
                new_symbols |= set(n.new_symbols(sdfg, self, {}).keys())
            elif isinstance(n, nd.AccessNode):
                # Add data descriptor symbols
                freesyms |= set(map(str, n.desc(sdfg).used_symbols(all_symbols)))
            elif isinstance(n, nd.Tasklet):
                if n.language == dtypes.Language.Python:
                    # Consider callbacks defined as symbols as free
                    for stmt in n.code.code:
                        for astnode in ast.walk(stmt):
                            if (isinstance(astnode, ast.Call) and isinstance(astnode.func, ast.Name)
                                    and astnode.func.id in sdfg.symbols):
                                freesyms.add(astnode.func.id)
                else:
                    # Find all string tokens and filter them to sdfg.symbols, while ignoring connectors
                    codesyms = symbolic.symbols_in_code(
                        n.code.as_string,
                        potential_symbols=sdfg.symbols.keys(),
                        symbols_to_ignore=(n.in_connectors.keys() | n.out_connectors.keys() | n.ignored_symbols),
                    )
                    freesyms |= codesyms
                    continue

            if hasattr(n, 'used_symbols'):
                freesyms |= n.used_symbols(all_symbols)
            else:
                freesyms |= n.free_symbols

        # Free symbols from memlets
        for e in self.edges():
            # If used for code generation, only consider memlet tree leaves
            if not all_symbols and not self.is_leaf_memlet(e):
                continue

            freesyms |= e.data.used_symbols(all_symbols, e)

        # Do not consider SDFG constants as symbols
        new_symbols.update(set(sdfg.constants.keys()))
        return freesyms - new_symbols

    @property
    def free_symbols(self) -> Set[str]:
        """
        Returns a set of symbol names that are used, but not defined, in
        this graph view (SDFG state or subgraph thereof).

        :note: Assumes that the graph is valid (i.e., without undefined or
               overlapping symbols).
        """
        return self.used_symbols(all_symbols=True)

    def defined_symbols(self) -> Dict[str, dt.Data]:
        """
        Returns a dictionary that maps currently-defined symbols in this SDFG
        state or subgraph to their types.
        """
        state = self.graph if isinstance(self, SubgraphView) else self
        sdfg = state.sdfg

        # Start with SDFG global symbols
        defined_syms = {k: v for k, v in sdfg.symbols.items()}

        def update_if_not_none(dic, update):
            update = {k: v for k, v in update.items() if v is not None}
            dic.update(update)

        # Add data-descriptor free symbols
        for desc in sdfg.arrays.values():
            for sym in desc.free_symbols:
                if sym.dtype is not None:
                    defined_syms[str(sym)] = sym.dtype

        # Add inter-state symbols
        for edge in sdfg.dfs_edges(sdfg.start_state):
            update_if_not_none(defined_syms, edge.data.new_symbols(sdfg, defined_syms))

        # Add scope symbols all the way to the subgraph
        sdict = state.scope_dict()
        scope_nodes = []
        for source_node in self.source_nodes():
            curnode = source_node
            while sdict[curnode] is not None:
                curnode = sdict[curnode]
                scope_nodes.append(curnode)

        for snode in dtypes.deduplicate(list(reversed(scope_nodes))):
            update_if_not_none(defined_syms, snode.new_symbols(sdfg, state, defined_syms))

        return defined_syms

    def _read_and_write_sets(self) -> Tuple[Dict[AnyStr, List[Subset]], Dict[AnyStr, List[Subset]]]:
        """
        Determines what data is read and written in this subgraph, returning
        dictionaries from data containers to all subsets that are read/written.
        """
        read_set = collections.defaultdict(list)
        write_set = collections.defaultdict(list)
        from dace.sdfg import utils  # Avoid cyclic import
        subgraphs = utils.concurrent_subgraphs(self)
        for sg in subgraphs:
            rs = collections.defaultdict(list)
            ws = collections.defaultdict(list)
            # Traverse in topological order, so data that is written before it
            # is read is not counted in the read set
            for n in utils.dfs_topological_sort(sg, sources=sg.source_nodes()):
                if isinstance(n, nd.AccessNode):
                    in_edges = sg.in_edges(n)
                    out_edges = sg.out_edges(n)
                    # Filter out memlets which go out but the same data is written to the AccessNode by another memlet
                    for out_edge in list(out_edges):
                        for in_edge in list(in_edges):
                            if (in_edge.data.data == out_edge.data.data
                                    and in_edge.data.dst_subset.covers(out_edge.data.src_subset)):
                                out_edges.remove(out_edge)
                                break

                    for e in in_edges:
                        # skip empty memlets
                        if e.data.is_empty():
                            continue
                        # Store all subsets that have been written
                        ws[n.data].append(e.data.subset)
                    for e in out_edges:
                        # skip empty memlets
                        if e.data.is_empty():
                            continue
                        rs[n.data].append(e.data.subset)
            # Union all subgraphs, so an array that was excluded from the read
            # set because it was written first is still included if it is read
            # in another subgraph
            for data, accesses in rs.items():
                read_set[data] += accesses
            for data, accesses in ws.items():
                write_set[data] += accesses
        return read_set, write_set

    def read_and_write_sets(self) -> Tuple[Set[AnyStr], Set[AnyStr]]:
        """
        Determines what data is read and written in this subgraph.
        
        :return: A two-tuple of sets of things denoting
                 ({data read}, {data written}).
        """
        read_set, write_set = self._read_and_write_sets()
        return set(read_set.keys()), set(write_set.keys())

    def unordered_arglist(self,
                          defined_syms=None,
                          shared_transients=None) -> Tuple[Dict[str, dt.Data], Dict[str, dt.Data]]:
        sdfg: 'SDFG' = self.sdfg
        shared_transients = shared_transients or sdfg.shared_transients()
        sdict = self.scope_dict()

        data_args = {}
        scalar_args = {}

        # Gather data descriptors from nodes
        descs = {}
        descs_with_nodes = {}
        scalars_with_nodes = set()
        for node in self.nodes():
            if isinstance(node, nd.AccessNode):
                descs[node.data] = node.desc(sdfg)
                descs_with_nodes[node.data] = node
                if isinstance(node.desc(sdfg), dt.Scalar):
                    scalars_with_nodes.add(node.data)

        # If a subgraph, and a node appears outside the subgraph as well,
        # it is externally allocated
        if isinstance(self, SubgraphView):
            outer_nodes = set(self.graph.nodes()) - set(self.nodes())
            for node in outer_nodes:
                if isinstance(node, nd.AccessNode) and node.data in descs:
                    desc = descs[node.data]
                    if isinstance(desc, dt.Scalar):
                        scalar_args[node.data] = desc
                    else:
                        data_args[node.data] = desc

        # Add data arguments from memlets, if do not appear in any of the nodes
        # (i.e., originate externally)
        for edge in self.edges():
            if edge.data.data is not None and edge.data.data not in descs:
                desc = sdfg.arrays[edge.data.data]
                if isinstance(desc, dt.Scalar):
                    # Ignore code->code edges.
                    if (isinstance(edge.src, nd.CodeNode) and isinstance(edge.dst, nd.CodeNode)):
                        continue

                    scalar_args[edge.data.data] = desc
                else:
                    data_args[edge.data.data] = desc

        # Loop over locally-used data descriptors
        for name, desc in descs.items():
            if name in data_args or name in scalar_args:
                continue
            # If scalar, always add if there are no scalar nodes
            if isinstance(desc, dt.Scalar) and name not in scalars_with_nodes:
                scalar_args[name] = desc
            # If array/stream is not transient, then it is external
            elif not desc.transient:
                data_args[name] = desc
            # Check for shared transients
            elif name in shared_transients:
                data_args[name] = desc
            # Check allocation lifetime for external transients:
            #   1. If a full state, Global, SDFG, and Persistent
            elif (not isinstance(self, SubgraphView)
                  and desc.lifetime not in (dtypes.AllocationLifetime.Scope, dtypes.AllocationLifetime.State)):
                data_args[name] = desc
            #   2. If a subgraph, State also applies
            elif isinstance(self, SubgraphView):
                if (desc.lifetime != dtypes.AllocationLifetime.Scope):
                    data_args[name] = desc
                # Check for allocation constraints that would
                # enforce array to be allocated outside subgraph
                elif desc.lifetime == dtypes.AllocationLifetime.Scope:
                    curnode = sdict[descs_with_nodes[name]]
                    while curnode is not None:
                        if dtypes.can_allocate(desc.storage, curnode.schedule):
                            break
                        curnode = sdict[curnode]
                    else:
                        # If no internal scope can allocate node,
                        # mark as external
                        data_args[name] = desc
        # End of data descriptor loop

        # Add scalar arguments from free symbols
        defined_syms = defined_syms or self.defined_symbols()
        scalar_args.update({
            k: dt.Scalar(defined_syms[k]) if k in defined_syms else sdfg.arrays[k]
            for k in self.used_symbols(all_symbols=False) if not k.startswith('__dace') and k not in sdfg.constants
        })

        # Add scalar arguments from free symbols of data descriptors
        for arg in data_args.values():
            scalar_args.update({
                str(k): dt.Scalar(k.dtype)
                for k in arg.used_symbols(all_symbols=False)
                if not str(k).startswith('__dace') and str(k) not in sdfg.constants
            })

        return data_args, scalar_args

    def signature_arglist(self, with_types=True, for_call=False):
        """ Returns a list of arguments necessary to call this state or
            subgraph, formatted as a list of C definitions.

            :param with_types: If True, includes argument types in the result.
            :param for_call: If True, returns arguments that can be used when
                             calling the SDFG.
            :return: A list of strings. For example: `['float *A', 'int b']`.
        """
        return [v.as_arg(name=k, with_types=with_types, for_call=for_call) for k, v in self.arglist().items()]

    def scope_subgraph(self, entry_node, include_entry=True, include_exit=True):
        from dace.sdfg.scope import _scope_subgraph
        return _scope_subgraph(self, entry_node, include_entry, include_exit)

    def top_level_transients(self):
        """Iterate over top-level transients of this state."""
        schildren = self.scope_children()
        sdfg = self.sdfg
        result = set()
        for node in schildren[None]:
            if isinstance(node, nd.AccessNode) and node.desc(sdfg).transient:
                result.add(node.data)
        return result

    def all_transients(self) -> List[str]:
        """Iterate over all transients in this state."""
        return dtypes.deduplicate(
            [n.data for n in self.nodes() if isinstance(n, nd.AccessNode) and n.desc(self.sdfg).transient])

    def replace(self, name: str, new_name: str):
        """ Finds and replaces all occurrences of a symbol or array in this
            state.

            :param name: Name to find.
            :param new_name: Name to replace.
        """
        from dace.sdfg.replace import replace
        replace(self, name, new_name)

    def replace_dict(self,
                     repl: Dict[str, str],
                     symrepl: Optional[Dict[symbolic.SymbolicType, symbolic.SymbolicType]] = None):
        from dace.sdfg.replace import replace_dict
        replace_dict(self, repl, symrepl)


@make_properties
class ControlGraphView(BlockGraphView, abc.ABC):

    ###################################################################
    # Typing overrides

    @overload
    def nodes(self) -> List['ControlFlowBlock']:
        ...

    @overload
    def edges(self) -> List[Edge['dace.sdfg.InterstateEdge']]:
        ...

    ###################################################################
    # Traversal methods

    def all_nodes_recursive(self, predicate = None) -> Iterator[Tuple[NodeT, GraphT]]:
        for node in self.nodes():
            yield node, self
            if predicate is None or predicate(node, self):
                yield from node.all_nodes_recursive()

    def all_edges_recursive(self) -> Iterator[Tuple[EdgeT, GraphT]]:
        for e in self.edges():
            yield e, self
        for node in self.nodes():
            yield from node.all_edges_recursive()

    def data_nodes(self) -> List[nd.AccessNode]:
        data_nodes = []
        for node in self.nodes():
            data_nodes.extend(node.data_nodes())
        return data_nodes

    def entry_node(self, node: nd.Node) -> Optional[nd.EntryNode]:
        for block in self.nodes():
            if node in block.nodes():
                return block.exit_node(node)
        return None

    def exit_node(self, entry_node: nd.EntryNode) -> Optional[nd.ExitNode]:
        for block in self.nodes():
            if entry_node in block.nodes():
                return block.exit_node(entry_node)
        return None

    ###################################################################
    # Memlet-tracking methods

    def memlet_path(self, edge: MultiConnectorEdge[mm.Memlet]) -> List[MultiConnectorEdge[mm.Memlet]]:
        for block in self.nodes():
            if edge in block.edges():
                return block.memlet_path(edge)
        return []

    def memlet_tree(self, edge: MultiConnectorEdge) -> mm.MemletTree:
        for block in self.nodes():
            if edge in block.edges():
                return block.memlet_tree(edge)
        return mm.MemletTree(edge)

    def in_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        for block in self.nodes():
            if node in block.nodes():
                return block.in_edges_by_connector(node, connector)
        return []

    def out_edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        for block in self.nodes():
            if node in block.nodes():
                return block.out_edges_by_connector(node, connector)
        return []

    def edges_by_connector(self, node: nd.Node, connector: AnyStr) -> Iterable[MultiConnectorEdge[mm.Memlet]]:
        for block in self.nodes():
            if node in block.nodes():
                return block.edges_by_connector(node, connector)

    ###################################################################
    # Query, subgraph, and replacement methods

    @abc.abstractmethod
    def _used_symbols_internal(self,
                               all_symbols: bool,
                               defined_syms: Optional[Set] = None,
                               free_syms: Optional[Set] = None,
                               used_before_assignment: Optional[Set] = None,
                               keep_defined_in_mapping: bool = False) -> Tuple[Set[str], Set[str], Set[str]]:
        raise NotImplementedError()

    def used_symbols(self, all_symbols: bool, keep_defined_in_mapping: bool = False) -> Set[str]:
        return self._used_symbols_internal(all_symbols, keep_defined_in_mapping=keep_defined_in_mapping)[0]

    def read_and_write_sets(self) -> Tuple[Set[AnyStr], Set[AnyStr]]:
        read_set = set()
        write_set = set()
        for block in self.nodes():
            for edge in self.in_edges(block):
                read_set |= edge.data.free_symbols & self.sdfg.arrays.keys()
            rs, ws = block.read_and_write_sets()
            read_set.update(rs)
            write_set.update(ws)
        return read_set, write_set

    def unordered_arglist(self,
                          defined_syms=None,
                          shared_transients=None) -> Tuple[Dict[str, dt.Data], Dict[str, dt.Data]]:
        data_args = {}
        scalar_args = {}
        for block in self.nodes():
            n_data_args, n_scalar_args = block.unordered_arglist(defined_syms, shared_transients)
            data_args.update(n_data_args)
            scalar_args.update(n_scalar_args)
        return data_args, scalar_args

    def top_level_transients(self) -> Set[str]:
        res = set()
        for block in self.nodes():
            res.update(block.top_level_transients())
        return res

    def all_transients(self) -> List[str]:
        res = []
        for block in self.nodes():
            res.extend(block.all_transients())
        return dtypes.deduplicate(res)

    def replace(self, name: str, new_name: str):
        for n in self.nodes():
            n.replace(name, new_name)

    def replace_dict(self,
                     repl: Dict[str, str],
                     symrepl: Optional[Dict[symbolic.SymbolicType, symbolic.SymbolicType]] = None,
                     replace_in_graph: bool = True,
                     replace_keys: bool = False):
        symrepl = symrepl or {
            symbolic.symbol(k): symbolic.pystr_to_symbolic(v) if isinstance(k, str) else v
            for k, v in repl.items()
        }

        if replace_in_graph:
            # Replace in inter-state edges
            for edge in self.edges():
                edge.data.replace_dict(repl, replace_keys=replace_keys)

            # Replace in states
            for state in self.nodes():
                state.replace_dict(repl, symrepl)


@make_properties
class ControlFlowBlock(BlockGraphView, abc.ABC):

    is_collapsed = Property(dtype=bool, desc='Show this block as collapsed', default=False)

    pre_conditions = DictProperty(key_type=str, value_type=list, desc='Pre-conditions for this block')
    post_conditions = DictProperty(key_type=str, value_type=list, desc='Post-conditions for this block')
    invariant_conditions = DictProperty(key_type=str, value_type=list, desc='Invariant conditions for this block')

    _label: str

    _default_lineinfo: Optional[dace.dtypes.DebugInfo] = None
    _sdfg: Optional['SDFG'] = None
    _parent_graph: Optional['ControlFlowRegion'] = None

    def __init__(self, label: str = '', sdfg: Optional['SDFG'] = None, parent: Optional['ControlFlowRegion'] = None):
        super(ControlFlowBlock, self).__init__()
        self._label = label
        self._default_lineinfo = None
        self._sdfg = sdfg
        self._parent_graph = parent
        self.is_collapsed = False
        self.pre_conditions = {}
        self.post_conditions = {}
        self.invariant_conditions = {}

    def nodes(self):
        return []

    def edges(self):
        return []

    def set_default_lineinfo(self, lineinfo: dace.dtypes.DebugInfo):
        """
        Sets the default source line information to be lineinfo, or None to
        revert to default mode.
        """
        self._default_lineinfo = lineinfo

    def to_json(self, parent=None):
        tmp = {
            'type': self.__class__.__name__,
            'collapsed': self.is_collapsed,
            'label': self._label,
            'id': parent.node_id(self) if parent is not None else None,
            'attributes': serialize.all_properties_to_json(self),
        }
        return tmp

    @classmethod
    def from_json(cls, json_obj, context=None):
        context = context or {'sdfg': None, 'parent_graph': None}
        _type = json_obj['type']
        if _type != cls.__name__:
            raise TypeError("Class type mismatch")

        ret = cls(label=json_obj['label'], sdfg=context['sdfg'])

        dace.serialize.set_properties_from_json(ret, json_obj)

        return ret

    def __str__(self):
        return self._label

    def __repr__(self) -> str:
        return f'ControlFlowBlock ({self.label})'

    def __deepcopy__(self, memo):
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            if k in ('_parent_graph', '_sdfg'):  # Skip derivative attributes
                continue
            setattr(result, k, copy.deepcopy(v, memo))

        for k in ('_parent_graph', '_sdfg'):
            if id(getattr(self, k)) in memo:
                setattr(result, k, memo[id(getattr(self, k))])
            else:
                setattr(result, k, None)

        return result

    @property
    def label(self) -> str:
        return self._label

    @label.setter
    def label(self, label: str):
        self._label = label

    @property
    def name(self) -> str:
        return self._label

    @property
    def sdfg(self) -> 'SDFG':
        return self._sdfg

    @sdfg.setter
    def sdfg(self, sdfg: 'SDFG'):
        self._sdfg = sdfg

    @property
    def parent_graph(self) -> 'ControlFlowRegion':
        return self._parent_graph

    @parent_graph.setter
    def parent_graph(self, parent: Optional['ControlFlowRegion']):
        self._parent_graph = parent

    @property
    def block_id(self) -> int:
        return self.parent_graph.node_id(self)


@make_properties
class SDFGState(OrderedMultiDiConnectorGraph[nd.Node, mm.Memlet], ControlFlowBlock, DataflowGraphView):
    """ An acyclic dataflow multigraph in an SDFG, corresponding to a
        single state in the SDFG state machine. """

    nosync = Property(dtype=bool, default=False, desc="Do not synchronize at the end of the state")

    instrument = EnumProperty(dtype=dtypes.InstrumentationType,
                              desc="Measure execution statistics with given method",
                              default=dtypes.InstrumentationType.No_Instrumentation)

    symbol_instrument = EnumProperty(dtype=dtypes.DataInstrumentationType,
                                     desc="Instrument symbol values when this state is executed",
                                     default=dtypes.DataInstrumentationType.No_Instrumentation)
    symbol_instrument_condition = CodeProperty(desc="Condition under which to trigger the symbol instrumentation",
                                               default=CodeBlock("1", language=dtypes.Language.CPP))

    executions = SymbolicProperty(default=0,
                                  desc="The number of times this state gets "
                                  "executed (0 stands for unbounded)")
    dynamic_executions = Property(dtype=bool, default=True, desc="The number of executions of this state "
                                  "is dynamic")

    ranges = DictProperty(key_type=symbolic.symbol,
                          value_type=Range,
                          default={},
                          desc='Variable ranges, typically within loops')

    location = DictProperty(key_type=str,
                            value_type=symbolic.pystr_to_symbolic,
                            desc='Full storage location identifier (e.g., rank, GPU ID)')

    def __repr__(self) -> str:
        return f"SDFGState ({self.label})"

    def __init__(self, label=None, sdfg=None, debuginfo=None, location=None):
        """ Constructs an SDFG state.

            :param label: Name for the state (optional).
            :param sdfg: A reference to the parent SDFG.
            :param debuginfo: Source code locator for debugging.
        """
        OrderedMultiDiConnectorGraph.__init__(self)
        ControlFlowBlock.__init__(self, label, sdfg)
        super(SDFGState, self).__init__()
        self._label = label
        self._graph = self  # Allowing MemletTrackingView mixin to work
        self._clear_scopedict_cache()
        self._debuginfo = debuginfo
        self.nosync = False
        self.location = location if location is not None else {}
        self._default_lineinfo = None

    @property
    def parent(self):
        """ Returns the parent SDFG of this state. """
        return self.sdfg

    @parent.setter
    def parent(self, value):
        self.sdfg = value

    def is_empty(self):
        return self.number_of_nodes() == 0

    def validate(self) -> None:
        validate_state(self)

    def nodes(self) -> List[nd.Node]:  # Added for type hints
        return super().nodes()

    def all_edges_and_connectors(self, *nodes):
        """
        Returns an iterable to incoming and outgoing Edge objects, along
        with their connector types.
        """
        for node in nodes:
            for e in self.in_edges(node):
                yield e, (node.in_connectors[e.dst_conn] if e.dst_conn else None)
            for e in self.out_edges(node):
                yield e, (node.out_connectors[e.src_conn] if e.src_conn else None)

    def add_node(self, node):
        if not isinstance(node, nd.Node):
            raise TypeError("Expected Node, got " + type(node).__name__ + " (" + str(node) + ")")
        # Correct nested SDFG's parent attributes
        if isinstance(node, nd.NestedSDFG):
            node.sdfg.parent = self
            node.sdfg.parent_sdfg = self.sdfg
            node.sdfg.parent_nsdfg_node = node
        self._clear_scopedict_cache()
        return super(SDFGState, self).add_node(node)

    def remove_node(self, node):
        self._clear_scopedict_cache()
        super(SDFGState, self).remove_node(node)

    def add_edge(self, u, u_connector, v, v_connector, memlet):
        if not isinstance(u, nd.Node):
            raise TypeError("Source node is not of type nd.Node (type: %s)" % str(type(u)))
        if u_connector is not None and not isinstance(u_connector, str):
            raise TypeError("Source connector is not string (type: %s)" % str(type(u_connector)))
        if not isinstance(v, nd.Node):
            raise TypeError("Destination node is not of type nd.Node (type: " + "%s)" % str(type(v)))
        if v_connector is not None and not isinstance(v_connector, str):
            raise TypeError("Destination connector is not string (type: %s)" % str(type(v_connector)))
        if not isinstance(memlet, mm.Memlet):
            raise TypeError("Memlet is not of type Memlet (type: %s)" % str(type(memlet)))

        if u_connector and isinstance(u, nd.AccessNode) and u_connector not in u.out_connectors:
            u.add_out_connector(u_connector, force=True)
        if v_connector and isinstance(v, nd.AccessNode) and v_connector not in v.in_connectors:
            v.add_in_connector(v_connector, force=True)

        self._clear_scopedict_cache()
        result = super(SDFGState, self).add_edge(u, u_connector, v, v_connector, memlet)
        memlet.try_initialize(self.sdfg, self, result)
        return result

    def remove_edge(self, edge):
        self._clear_scopedict_cache()
        super(SDFGState, self).remove_edge(edge)

    def remove_edge_and_connectors(self, edge):
        self._clear_scopedict_cache()
        super(SDFGState, self).remove_edge(edge)
        if edge.src_conn in edge.src.out_connectors:
            edge.src.remove_out_connector(edge.src_conn)
        if edge.dst_conn in edge.dst.in_connectors:
            edge.dst.remove_in_connector(edge.dst_conn)

    def to_json(self, parent=None):
        # Create scope dictionary with a failsafe
        try:
            scope_dict = {k: sorted(v) for k, v in sorted(self.scope_children(return_ids=True).items())}
        except (RuntimeError, ValueError):
            scope_dict = {}

        # Try to initialize edges before serialization
        for edge in self.edges():
            edge.data.try_initialize(self.sdfg, self, edge)

        ret = {
            'type': type(self).__name__,
            'label': self.name,
            'id': parent.node_id(self) if parent is not None else None,
            'collapsed': self.is_collapsed,
            'scope_dict': scope_dict,
            'nodes': [n.to_json(self) for n in self.nodes()],
            'edges':
            [e.to_json(self) for e in sorted(self.edges(), key=lambda e: (e.src_conn or '', e.dst_conn or ''))],
            'attributes': serialize.all_properties_to_json(self),
        }

        return ret

    @classmethod
    def from_json(cls, json_obj, context={'sdfg': None}, pre_ret=None):
        """ Loads the node properties, label and type into a dict.

            :param json_obj: The object containing information about this node.
                             NOTE: This may not be a string!
            :return: An SDFGState instance constructed from the passed data
        """

        _type = json_obj['type']
        if _type != cls.__name__:
            raise Exception("Class type mismatch")

        attrs = json_obj['attributes']
        nodes = json_obj['nodes']
        edges = json_obj['edges']

        ret = pre_ret if pre_ret is not None else SDFGState(label=json_obj['label'],
                                                            sdfg=context['sdfg'],
                                                            debuginfo=None)

        rec_ci = {
            'sdfg': context['sdfg'],
            'sdfg_state': ret,
            'callback': context['callback'] if 'callback' in context else None
        }
        serialize.set_properties_from_json(ret, json_obj, rec_ci)

        for n in nodes:
            nret = serialize.from_json(n, context=rec_ci)
            ret.add_node(nret)

        # Connect using the edges
        for e in edges:
            eret = serialize.from_json(e, context=rec_ci)

            ret.add_edge(eret.src, eret.src_conn, eret.dst, eret.dst_conn, eret.data)

        # Fix potentially broken scopes
        for n in nodes:
            if isinstance(n, nd.MapExit):
                n.map = ret.entry_node(n).map
            elif isinstance(n, nd.ConsumeExit):
                n.consume = ret.entry_node(n).consume

        # Reinitialize memlets
        for edge in ret.edges():
            edge.data.try_initialize(context['sdfg'], ret, edge)

        return ret

    def _repr_html_(self):
        """ HTML representation of a state, used mainly for Jupyter
            notebooks. """
        # Create dummy SDFG with this state as the only one
        from dace.sdfg import SDFG
        arrays = set(n.data for n in self.data_nodes())
        sdfg = SDFG(self.label)
        sdfg._arrays = {k: self.sdfg.arrays[k] for k in arrays}
        sdfg.add_node(self)

        return sdfg._repr_html_()

    def __deepcopy__(self, memo):
        result: SDFGState = ControlFlowBlock.__deepcopy__(self, memo)

        for node in result.nodes():
            if isinstance(node, nd.NestedSDFG):
                try:
                    node.sdfg.parent = result
                except AttributeError:
                    # NOTE: There are cases where a NestedSDFG does not have `sdfg` attribute.
                    # TODO: Investigate why this happens.
                    pass
        return result

    def symbols_defined_at(self, node: nd.Node) -> Dict[str, dtypes.typeclass]:
        """
        Returns all symbols available to a given node.
        The symbols a node can access are a combination of the global SDFG
        symbols, symbols defined in inter-state paths to its state,
        and symbols defined in scope entries in the path to this node.

        :param node: The given node.
        :return: A dictionary mapping symbol names to their types.
        """
        from dace.sdfg.sdfg import SDFG

        if node is None:
            return collections.OrderedDict()

        sdfg: SDFG = self.sdfg

        # Start with global symbols
        symbols = collections.OrderedDict(sdfg.symbols)
        for desc in sdfg.arrays.values():
            symbols.update([(str(s), s.dtype) for s in desc.free_symbols])

        # Add symbols from inter-state edges along the path to the state
        try:
            start_state = sdfg.start_state
            for e in sdfg.predecessor_state_transitions(start_state):
                symbols.update(e.data.new_symbols(sdfg, symbols))
        except ValueError:
            # Cannot determine starting state (possibly some inter-state edges
            # do not yet exist)
            for e in sdfg.edges():
                symbols.update(e.data.new_symbols(sdfg, symbols))

        # Find scopes this node is situated in
        sdict = self.scope_dict()
        scope_list = []
        curnode = node
        while sdict[curnode] is not None:
            curnode = sdict[curnode]
            scope_list.append(curnode)

        # Add the scope symbols top-down
        for scope_node in reversed(scope_list):
            symbols.update(scope_node.new_symbols(sdfg, self, symbols))

        return symbols

    # Dynamic SDFG creation API
    ##############################
    def add_read(self, array_or_stream_name: str, debuginfo: Optional[dtypes.DebugInfo] = None) -> nd.AccessNode:
        """
        Adds an access node to this SDFG state (alias of ``add_access``).

        :param array_or_stream_name: The name of the array/stream.
        :param debuginfo: Source line information for this access node.
        :return: An array access node.
        :see: add_access
        """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        return self.add_access(array_or_stream_name, debuginfo=debuginfo)

    def add_write(self, array_or_stream_name: str, debuginfo: Optional[dtypes.DebugInfo] = None) -> nd.AccessNode:
        """
        Adds an access node to this SDFG state (alias of ``add_access``).

        :param array_or_stream_name: The name of the array/stream.
        :param debuginfo: Source line information for this access node.
        :return: An array access node.
        :see: add_access
        """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        return self.add_access(array_or_stream_name, debuginfo=debuginfo)

    def add_access(self, array_or_stream_name: str, debuginfo: Optional[dtypes.DebugInfo] = None) -> nd.AccessNode:
        """ Adds an access node to this SDFG state.

            :param array_or_stream_name: The name of the array/stream.
            :param debuginfo: Source line information for this access node.
            :return: An array access node.
        """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        node = nd.AccessNode(array_or_stream_name, debuginfo=debuginfo)
        self.add_node(node)
        return node

    def add_tasklet(
        self,
        name: str,
        inputs: Union[Set[str], Dict[str, dtypes.typeclass]],
        outputs: Union[Set[str], Dict[str, dtypes.typeclass]],
        code: str,
        language: dtypes.Language = dtypes.Language.Python,
        state_fields: Optional[List[str]] = None,
        code_global: str = "",
        code_init: str = "",
        code_exit: str = "",
        location: dict = None,
        side_effects: Optional[bool] = None,
        debuginfo=None,
    ):
        """ Adds a tasklet to the SDFG state. """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)

        # Make dictionary of autodetect connector types from set
        if isinstance(inputs, (set, collections.abc.KeysView)):
            inputs = {k: None for k in inputs}
        if isinstance(outputs, (set, collections.abc.KeysView)):
            outputs = {k: None for k in outputs}

        tasklet = nd.Tasklet(
            name,
            inputs,
            outputs,
            code,
            language,
            state_fields=state_fields,
            code_global=code_global,
            code_init=code_init,
            code_exit=code_exit,
            location=location,
            side_effects=side_effects,
            debuginfo=debuginfo,
        ) if language != dtypes.Language.SystemVerilog else nd.RTLTasklet(
            name,
            inputs,
            outputs,
            code,
            language,
            state_fields=state_fields,
            code_global=code_global,
            code_init=code_init,
            code_exit=code_exit,
            location=location,
            side_effects=side_effects,
            debuginfo=debuginfo,
        )
        self.add_node(tasklet)
        return tasklet

    def add_nested_sdfg(
        self,
        sdfg: 'SDFG',
        parent,
        inputs: Union[Set[str], Dict[str, dtypes.typeclass]],
        outputs: Union[Set[str], Dict[str, dtypes.typeclass]],
        symbol_mapping: Dict[str, Any] = None,
        name=None,
        schedule=dtypes.ScheduleType.Default,
        location=None,
        debuginfo=None,
    ):
        """ Adds a nested SDFG to the SDFG state. """
        if name is None:
            name = sdfg.label
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)

        sdfg.parent = self
        sdfg.parent_sdfg = self.sdfg

        sdfg.update_cfg_list([])

        # Make dictionary of autodetect connector types from set
        if isinstance(inputs, (set, collections.abc.KeysView)):
            inputs = {k: None for k in inputs}
        if isinstance(outputs, (set, collections.abc.KeysView)):
            outputs = {k: None for k in outputs}

        s = nd.NestedSDFG(
            name,
            sdfg,
            inputs,
            outputs,
            symbol_mapping=symbol_mapping,
            schedule=schedule,
            location=location,
            debuginfo=debuginfo,
        )
        self.add_node(s)

        sdfg.parent_nsdfg_node = s

        # Add "default" undefined symbols if None are given
        symbols = sdfg.free_symbols
        if symbol_mapping is None:
            symbol_mapping = {s: s for s in symbols}
            s.symbol_mapping = symbol_mapping

        # Validate missing symbols
        missing_symbols = [s for s in symbols if s not in symbol_mapping]
        if missing_symbols and parent:
            # If symbols are missing, try to get them from the parent SDFG
            parent_mapping = {s: s for s in missing_symbols if s in parent.symbols}
            symbol_mapping.update(parent_mapping)
            s.symbol_mapping = symbol_mapping
            missing_symbols = [s for s in symbols if s not in symbol_mapping]
        if missing_symbols:
            raise ValueError('Missing symbols on nested SDFG "%s": %s' % (name, missing_symbols))

        # Add new global symbols to nested SDFG
        from dace.codegen.tools.type_inference import infer_expr_type
        for sym, symval in s.symbol_mapping.items():
            if sym not in sdfg.symbols:
                # TODO: Think of a better way to avoid calling
                # symbols_defined_at in this moment
                sdfg.add_symbol(sym, infer_expr_type(symval, self.sdfg.symbols) or dtypes.typeclass(int))

        return s

    def add_map(
        self,
        name,
        ndrange: Union[Dict[str, Union[str, sbs.Subset]], List[Tuple[str, Union[str, sbs.Subset]]]],
        schedule=dtypes.ScheduleType.Default,
        unroll=False,
        debuginfo=None,
    ) -> Tuple[nd.MapEntry, nd.MapExit]:
        """ Adds a map entry and map exit.

            :param name:      Map label
            :param ndrange:   Mapping between range variable names and their
                              subsets (parsed from strings)
            :param schedule:  Map schedule type
            :param unroll:    True if should unroll the map in code generation

            :return: (map_entry, map_exit) node 2-tuple
        """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        map = nd.Map(name, *_make_iterators(ndrange), schedule=schedule, unroll=unroll, debuginfo=debuginfo)
        map_entry = nd.MapEntry(map)
        map_exit = nd.MapExit(map)
        self.add_nodes_from([map_entry, map_exit])
        return map_entry, map_exit

    def add_consume(self,
                    name,
                    elements: Tuple[str, str],
                    condition: str = None,
                    schedule=dtypes.ScheduleType.Default,
                    chunksize=1,
                    debuginfo=None,
                    language=dtypes.Language.Python) -> Tuple[nd.ConsumeEntry, nd.ConsumeExit]:
        """ Adds consume entry and consume exit nodes.

            :param name:      Label
            :param elements:  A 2-tuple signifying the processing element
                              index and number of total processing elements
            :param condition: Quiescence condition to finish consuming, or
                              None (by default) to consume until the stream
                              is empty for the first time. If false, will
                              consume forever.
            :param schedule:  Consume schedule type.
            :param chunksize: Maximal number of elements to consume at a time.
            :param debuginfo: Source code line information for debugging.
            :param language:  Code language for ``condition``.

            :return: (consume_entry, consume_exit) node 2-tuple
        """
        if len(elements) != 2:
            raise TypeError("Elements must be a 2-tuple of "
                            "(PE_index, num_PEs)")
        pe_tuple = (elements[0], SymbolicProperty.from_string(elements[1]))

        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        if condition is not None:
            condition = CodeBlock(condition, language)
        consume = nd.Consume(name, pe_tuple, condition, schedule, chunksize, debuginfo=debuginfo)
        entry = nd.ConsumeEntry(consume)
        exit = nd.ConsumeExit(consume)

        self.add_nodes_from([entry, exit])
        return entry, exit

    def add_mapped_tasklet(self,
                           name: str,
                           map_ranges: Union[Dict[str, Union[str, sbs.Subset]], List[Tuple[str, Union[str,
                                                                                                      sbs.Subset]]]],
                           inputs: Dict[str, mm.Memlet],
                           code: str,
                           outputs: Dict[str, mm.Memlet],
                           schedule=dtypes.ScheduleType.Default,
                           unroll_map=False,
                           location=None,
                           language=dtypes.Language.Python,
                           debuginfo=None,
                           external_edges=False,
                           input_nodes: Optional[Dict[str, nd.AccessNode]] = None,
                           output_nodes: Optional[Dict[str, nd.AccessNode]] = None,
                           propagate=True) -> Tuple[nd.Tasklet, nd.MapEntry, nd.MapExit]:
        """ Convenience function that adds a map entry, tasklet, map exit,
            and the respective edges to external arrays.

            :param name:       Tasklet (and wrapping map) name
            :param map_ranges: Mapping between variable names and their
                               subsets
            :param inputs:     Mapping between input local variable names and
                               their memlets
            :param code:       Code (written in `language`)
            :param outputs:    Mapping between output local variable names and
                               their memlets
            :param schedule:   Map schedule
            :param unroll_map: True if map should be unrolled in code
                               generation
            :param location:   Execution location indicator.
            :param language:   Programming language in which the code is
                               written
            :param debuginfo:  Source line information
            :param external_edges: Create external access nodes and connect
                                   them with memlets automatically
            :param input_nodes: Mapping between data names and corresponding
                                input nodes to link to, if external_edges is
                                True.
            :param output_nodes: Mapping between data names and corresponding
                                 output nodes to link to, if external_edges is
                                 True.
            :param propagate: If True, computes outer memlets via propagation.
                              False will run faster but the SDFG may not be
                              semantically correct.
            :return: tuple of (tasklet, map_entry, map_exit)
        """
        map_name = name + "_map"
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)

        # Create appropriate dictionaries from inputs
        tinputs = {k: None for k, v in inputs.items()}
        toutputs = {k: None for k, v in outputs.items()}

        tasklet = nd.Tasklet(
            name,
            tinputs,
            toutputs,
            code,
            language=language,
            location=location,
            debuginfo=debuginfo,
        )
        map = nd.Map(map_name, *_make_iterators(map_ranges), schedule=schedule, unroll=unroll_map, debuginfo=debuginfo)
        map_entry = nd.MapEntry(map)
        map_exit = nd.MapExit(map)
        self.add_nodes_from([map_entry, tasklet, map_exit])

        # Create access nodes
        inpdict = {}
        outdict = {}
        if external_edges:
            input_nodes = input_nodes or {}
            output_nodes = output_nodes or {}
            input_data = dtypes.deduplicate([memlet.data for memlet in inputs.values()])
            output_data = dtypes.deduplicate([memlet.data for memlet in outputs.values()])
            for inp in sorted(input_data):
                if inp in input_nodes:
                    inpdict[inp] = input_nodes[inp]
                else:
                    inpdict[inp] = self.add_read(inp)
            for out in sorted(output_data):
                if out in output_nodes:
                    outdict[out] = output_nodes[out]
                else:
                    outdict[out] = self.add_write(out)

        edges: List[Edge[dace.Memlet]] = []

        # Connect inputs from map to tasklet
        tomemlet = {}
        for name, memlet in sorted(inputs.items()):
            # Set memlet local name
            memlet.name = name
            # Add internal memlet edge
            edges.append(self.add_edge(map_entry, None, tasklet, name, memlet))
            tomemlet[memlet.data] = memlet

        # If there are no inputs, add empty memlet
        if len(inputs) == 0:
            self.add_edge(map_entry, None, tasklet, None, mm.Memlet())

        if external_edges:
            for inp, inpnode in sorted(inpdict.items()):
                # Add external edge
                if propagate:
                    outer_memlet = propagate_memlet(self, tomemlet[inp], map_entry, True)
                else:
                    outer_memlet = tomemlet[inp]
                edges.append(self.add_edge(inpnode, None, map_entry, "IN_" + inp, outer_memlet))

                # Add connectors to internal edges
                for e in self.out_edges(map_entry):
                    if e.data.data == inp:
                        e._src_conn = "OUT_" + inp

                # Add connectors to map entry
                map_entry.add_in_connector("IN_" + inp)
                map_entry.add_out_connector("OUT_" + inp)

        # Connect outputs from tasklet to map
        tomemlet = {}
        for name, memlet in sorted(outputs.items()):
            # Set memlet local name
            memlet.name = name
            # Add internal memlet edge
            edges.append(self.add_edge(tasklet, name, map_exit, None, memlet))
            tomemlet[memlet.data] = memlet

        # If there are no outputs, add empty memlet
        if len(outputs) == 0:
            self.add_edge(tasklet, None, map_exit, None, mm.Memlet())

        if external_edges:
            for out, outnode in sorted(outdict.items()):
                # Add external edge
                if propagate:
                    outer_memlet = propagate_memlet(self, tomemlet[out], map_exit, True)
                else:
                    outer_memlet = tomemlet[out]
                edges.append(self.add_edge(map_exit, "OUT_" + out, outnode, None, outer_memlet))

                # Add connectors to internal edges
                for e in self.in_edges(map_exit):
                    if e.data.data == out:
                        e._dst_conn = "IN_" + out

                # Add connectors to map entry
                map_exit.add_in_connector("IN_" + out)
                map_exit.add_out_connector("OUT_" + out)

        # Try to initialize memlets
        for edge in edges:
            edge.data.try_initialize(self.sdfg, self, edge)

        return tasklet, map_entry, map_exit

    def add_reduce(
        self,
        wcr,
        axes,
        identity=None,
        schedule=dtypes.ScheduleType.Default,
        debuginfo=None,
    ) -> 'dace.libraries.standard.Reduce':
        """ Adds a reduction node.

            :param wcr: A lambda function representing the reduction operation
            :param axes: A tuple of axes to reduce the input memlet from, or
                         None for all axes
            :param identity: If not None, initializes output memlet values
                                 with this value
            :param schedule: Reduction schedule type

            :return: A Reduce node
        """
        import dace.libraries.standard as stdlib  # Avoid import loop
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        result = stdlib.Reduce('Reduce', wcr, axes, identity, schedule=schedule, debuginfo=debuginfo)
        self.add_node(result)
        return result

    def add_pipeline(self,
                     name,
                     ndrange,
                     init_size=0,
                     init_overlap=False,
                     drain_size=0,
                     drain_overlap=False,
                     additional_iterators={},
                     schedule=dtypes.ScheduleType.FPGA_Device,
                     debuginfo=None,
                     **kwargs) -> Tuple[nd.PipelineEntry, nd.PipelineExit]:
        """ Adds a pipeline entry and pipeline exit. These are used for FPGA
            kernels to induce distinct behavior between an "initialization"
            phase, a main streaming phase, and a "draining" phase, which require
            a additive number of extra loop iterations (i.e., N*M + I + D),
            where I and D are the number of initialization/drain iterations.
            The code can detect which phase it is in by querying the
            init_condition() and drain_condition() boolean variable.

            :param name:          Pipeline label
            :param ndrange:       Mapping between range variable names and
                                  their subsets (parsed from strings)
            :param init_size:     Number of iterations of initialization phase.
            :param init_overlap:  Whether the initialization phase overlaps
                                  with the "main" streaming phase of the loop.
            :param drain_size:    Number of iterations of draining phase.
            :param drain_overlap: Whether the draining phase overlaps with
                                  the "main" streaming phase of the loop.
            :param additional_iterators: a dictionary containing additional
                                  iterators that will be created for this scope and that are not
                                  automatically managed by the scope code.
                                  The dictionary takes the form 'variable_name' -> init_value
            :return: (map_entry, map_exit) node 2-tuple
        """
        debuginfo = _getdebuginfo(debuginfo or self._default_lineinfo)
        pipeline = nd.PipelineScope(name,
                                    *_make_iterators(ndrange),
                                    init_size=init_size,
                                    init_overlap=init_overlap,
                                    drain_size=drain_size,
                                    drain_overlap=drain_overlap,
                                    additional_iterators=additional_iterators,
                                    schedule=schedule,
                                    debuginfo=debuginfo,
                                    **kwargs)
        pipeline_entry = nd.PipelineEntry(pipeline)
        pipeline_exit = nd.PipelineExit(pipeline)
        self.add_nodes_from([pipeline_entry, pipeline_exit])
        return pipeline_entry, pipeline_exit

    def add_edge_pair(
        self,
        scope_node,
        internal_node,
        external_node,
        internal_memlet,
        external_memlet=None,
        scope_connector=None,
        internal_connector=None,
        external_connector=None,
    ):
        """ Adds two edges around a scope node (e.g., map entry, consume
            exit).

            The internal memlet (connecting to the internal node) has to be
            specified. If external_memlet (i.e., connecting to the node out
            of the scope) is not specified, it is propagated automatically
            using internal_memlet and the scope.

            :param scope_node: A scope node (for example, map exit) to add
                               edges around.
            :param internal_node: The node within the scope to connect to. If
                                  `scope_node` is an entry node, this means
                                  the node connected to the outgoing edge,
                                  else incoming.
            :param external_node: The node out of the scope to connect to.
            :param internal_memlet: The memlet on the edge to/from
                                    internal_node.
            :param external_memlet: The memlet on the edge to/from
                                    external_node (optional, will propagate
                                    internal_memlet if not specified).
            :param scope_connector: A scope connector name (or a unique
                                    number if not specified).
            :param internal_connector: The connector on internal_node to
                                       connect to.
            :param external_connector: The connector on external_node to
                                       connect to.
            :return: A 2-tuple representing the (internal, external) edges.
        """
        if not isinstance(scope_node, (nd.EntryNode, nd.ExitNode)):
            raise ValueError("scope_node is not a scope entry/exit")

        # Autodetermine scope connector ID
        if scope_connector is None:
            # Pick out numbered connectors that do not lead into the scope range
            conn_id = 1
            for conn in (scope_node.in_connectors.keys() | scope_node.out_connectors.keys()):
                if conn.startswith("IN_") or conn.startswith("OUT_"):
                    conn_name = conn[conn.find("_") + 1:]
                    try:
                        cid = int(conn_name)
                        if cid >= conn_id:
                            conn_id = cid + 1
                    except (TypeError, ValueError):
                        pass
            scope_connector = str(conn_id)

        # Add connectors
        scope_node.add_in_connector("IN_" + scope_connector)
        scope_node.add_out_connector("OUT_" + scope_connector)
        ##########################

        # Add internal edge
        if isinstance(scope_node, nd.EntryNode):
            iedge = self.add_edge(
                scope_node,
                "OUT_" + scope_connector,
                internal_node,
                internal_connector,
                internal_memlet,
            )
        else:
            iedge = self.add_edge(
                internal_node,
                internal_connector,
                scope_node,
                "IN_" + scope_connector,
                internal_memlet,
            )

        # Add external edge
        if external_memlet is None:
            # If undefined, propagate
            external_memlet = propagate_memlet(self, internal_memlet, scope_node, True)

        if isinstance(scope_node, nd.EntryNode):
            eedge = self.add_edge(
                external_node,
                external_connector,
                scope_node,
                "IN_" + scope_connector,
                external_memlet,
            )
        else:
            eedge = self.add_edge(
                scope_node,
                "OUT_" + scope_connector,
                external_node,
                external_connector,
                external_memlet,
            )

        # Try to initialize memlets
        iedge.data.try_initialize(self.sdfg, self, iedge)
        eedge.data.try_initialize(self.sdfg, self, eedge)

        return (iedge, eedge)

    def add_memlet_path(self, *path_nodes, memlet=None, src_conn=None, dst_conn=None, propagate=True):
        """
        Adds a path of memlet edges between the given nodes, propagating
        from the given innermost memlet.

        :param path_nodes: Nodes participating in the path (in the given order).
        :param memlet: (mandatory) The memlet at the innermost scope
                       (e.g., the incoming memlet to a tasklet (last
                       node), or an outgoing memlet from an array
                       (first node), followed by scope exits).
        :param src_conn: Connector at the beginning of the path.
        :param dst_conn: Connector at the end of the path.
        """
        if memlet is None:
            raise TypeError("Innermost memlet cannot be None")
        if len(path_nodes) < 2:
            raise ValueError("Memlet path must consist of at least 2 nodes")

        src_node = path_nodes[0]
        dst_node = path_nodes[-1]

        # Add edges first so that scopes can be understood
        edges = [
            self.add_edge(path_nodes[i], None, path_nodes[i + 1], None, mm.Memlet())
            for i in range(len(path_nodes) - 1)
        ]

        if not isinstance(memlet, mm.Memlet):
            raise TypeError("Expected Memlet, got: {}".format(type(memlet).__name__))

        if any(isinstance(n, nd.EntryNode) for n in path_nodes):
            propagate_forward = False
        else:  # dst node's scope is higher than src node, propagate out
            propagate_forward = True

        # Innermost edge memlet
        cur_memlet = memlet

        cur_memlet._is_data_src = (isinstance(src_node, nd.AccessNode) and src_node.data == cur_memlet.data)

        # Verify that connectors exist
        if (not memlet.is_empty() and hasattr(edges[0].src, "out_connectors") and isinstance(edges[0].src, nd.CodeNode)
                and not isinstance(edges[0].src, nd.LibraryNode)
                and (src_conn is None or src_conn not in edges[0].src.out_connectors)):
            raise ValueError("Output connector {} does not exist in {}".format(src_conn, edges[0].src.label))
        if (not memlet.is_empty() and hasattr(edges[-1].dst, "in_connectors")
                and isinstance(edges[-1].dst, nd.CodeNode) and not isinstance(edges[-1].dst, nd.LibraryNode)
                and (dst_conn is None or dst_conn not in edges[-1].dst.in_connectors)):
            raise ValueError("Input connector {} does not exist in {}".format(dst_conn, edges[-1].dst.label))

        path = edges if propagate_forward else reversed(edges)
        last_conn = None
        # Propagate and add edges
        for i, edge in enumerate(path):
            # Figure out source and destination connectors
            if propagate_forward:
                next_conn = edge.dst.next_connector(memlet.data)
                sconn = src_conn if i == 0 else "OUT_" + last_conn
                dconn = dst_conn if i == len(edges) - 1 else "IN_" + next_conn
            else:
                next_conn = edge.src.next_connector(memlet.data)
                sconn = src_conn if i == len(edges) - 1 else "OUT_" + next_conn
                dconn = dst_conn if i == 0 else "IN_" + last_conn

            last_conn = next_conn

            if cur_memlet.is_empty():
                if propagate_forward:
                    sconn = src_conn if i == 0 else None
                    dconn = dst_conn if i == len(edges) - 1 else None
                else:
                    sconn = src_conn if i == len(edges) - 1 else None
                    dconn = dst_conn if i == 0 else None

            # Modify edge to match memlet path
            edge._src_conn = sconn
            edge._dst_conn = dconn
            edge._data = cur_memlet

            # Add connectors to edges
            if propagate_forward:
                if dconn is not None:
                    edge.dst.add_in_connector(dconn)
                if sconn is not None:
                    edge.src.add_out_connector(sconn)
            else:
                if dconn is not None:
                    edge.dst.add_in_connector(dconn)
                if sconn is not None:
                    edge.src.add_out_connector(sconn)

            # Propagate current memlet to produce the next one
            if i < len(edges) - 1:
                snode = edge.dst if propagate_forward else edge.src
                if not cur_memlet.is_empty():
                    if propagate:
                        cur_memlet = propagate_memlet(self, cur_memlet, snode, True)
        # Try to initialize memlets
        for edge in edges:
            edge.data.try_initialize(self.sdfg, self, edge)

    def remove_memlet_path(self, edge: MultiConnectorEdge, remove_orphans: bool = True) -> None:
        """ Removes all memlets and associated connectors along a path formed
            by a given edge. Undefined behavior if the path is ambiguous.
            Orphaned entry and exit nodes will be connected with empty edges to
            maintain connectivity of the graph.

            :param edge: An edge that is part of the path that should be
                         removed, which will be passed to `memlet_path` to
                         determine the edges to be removed.
            :param remove_orphans: Remove orphaned data nodes from the graph if
                                   they become orphans from removing this memlet
                                   path.
        """

        path = self.memlet_path(edge)

        is_read = isinstance(path[0].src, nd.AccessNode)
        if is_read:
            # Traverse from connector to access node, so we can check if it's
            # safe to delete edges going out of a scope
            path = reversed(path)

        for edge in path:

            self.remove_edge(edge)

            # Check if there are any other edges exiting the source node that
            # use the same connector
            for e in self.out_edges(edge.src):
                if e.src_conn is not None and e.src_conn == edge.src_conn:
                    other_outgoing = True
                    break
            else:
                other_outgoing = False
                edge.src.remove_out_connector(edge.src_conn)

            # Check if there are any other edges entering the destination node
            # that use the same connector
            for e in self.in_edges(edge.dst):
                if e.dst_conn is not None and e.dst_conn == edge.dst_conn:
                    other_incoming = True
                    break
            else:
                other_incoming = False
                edge.dst.remove_in_connector(edge.dst_conn)

            if isinstance(edge.src, nd.EntryNode):
                # If removing this edge orphans the entry node, replace the
                # edge with an empty edge
                # NOTE: The entry node is an orphan iff it has no other outgoing edges.
                if self.out_degree(edge.src) == 0:
                    self.add_nedge(edge.src, edge.dst, mm.Memlet())
                if other_outgoing:
                    # If other inner memlets use the outer memlet, we have to
                    # stop the deletion here
                    break

            if isinstance(edge.dst, nd.ExitNode):
                # If removing this edge orphans the exit node, replace the
                # edge with an empty edge
                # NOTE: The exit node is an orphan iff it has no other incoming edges.
                if self.in_degree(edge.dst) == 0:
                    self.add_nedge(edge.src, edge.dst, mm.Memlet())
                if other_incoming:
                    # If other inner memlets use the outer memlet, we have to
                    # stop the deletion here
                    break

            # Prune access nodes
            if remove_orphans:
                if (isinstance(edge.src, nd.AccessNode) and self.degree(edge.src) == 0):
                    self.remove_node(edge.src)
                if (isinstance(edge.dst, nd.AccessNode) and self.degree(edge.dst) == 0):
                    self.remove_node(edge.dst)

    # DEPRECATED FUNCTIONS
    ######################################
    def add_array(self,
                  name,
                  shape,
                  dtype,
                  storage=dtypes.StorageType.Default,
                  transient=False,
                  strides=None,
                  offset=None,
                  lifetime=dtypes.AllocationLifetime.Scope,
                  debuginfo=None,
                  total_size=None,
                  find_new_name=False,
                  alignment=0):
        """ :note: This function is deprecated. """
        warnings.warn(
            'The "SDFGState.add_array" API is deprecated, please '
            'use "SDFG.add_array" and "SDFGState.add_access"', DeprecationWarning)
        # Workaround to allow this legacy API
        if name in self.sdfg._arrays:
            del self.sdfg._arrays[name]
        self.sdfg.add_array(name,
                            shape,
                            dtype,
                            storage=storage,
                            transient=transient,
                            strides=strides,
                            offset=offset,
                            lifetime=lifetime,
                            debuginfo=debuginfo,
                            find_new_name=find_new_name,
                            total_size=total_size,
                            alignment=alignment)
        return self.add_access(name, debuginfo)

    def add_stream(
        self,
        name,
        dtype,
        buffer_size=1,
        shape=(1, ),
        storage=dtypes.StorageType.Default,
        transient=False,
        offset=None,
        lifetime=dtypes.AllocationLifetime.Scope,
        debuginfo=None,
    ):
        """ :note: This function is deprecated. """
        warnings.warn(
            'The "SDFGState.add_stream" API is deprecated, please '
            'use "SDFG.add_stream" and "SDFGState.add_access"', DeprecationWarning)
        # Workaround to allow this legacy API
        if name in self.sdfg._arrays:
            del self.sdfg._arrays[name]
        self.sdfg.add_stream(
            name,
            dtype,
            buffer_size,
            shape,
            storage,
            transient,
            offset,
            lifetime,
            debuginfo,
        )
        return self.add_access(name, debuginfo)

    def add_scalar(
        self,
        name,
        dtype,
        storage=dtypes.StorageType.Default,
        transient=False,
        lifetime=dtypes.AllocationLifetime.Scope,
        debuginfo=None,
    ):
        """ :note: This function is deprecated. """
        warnings.warn(
            'The "SDFGState.add_scalar" API is deprecated, please '
            'use "SDFG.add_scalar" and "SDFGState.add_access"', DeprecationWarning)
        # Workaround to allow this legacy API
        if name in self.sdfg._arrays:
            del self.sdfg._arrays[name]
        self.sdfg.add_scalar(name, dtype, storage, transient, lifetime, debuginfo)
        return self.add_access(name, debuginfo)

    def add_transient(self,
                      name,
                      shape,
                      dtype,
                      storage=dtypes.StorageType.Default,
                      strides=None,
                      offset=None,
                      lifetime=dtypes.AllocationLifetime.Scope,
                      debuginfo=None,
                      total_size=None,
                      alignment=0):
        """ :note: This function is deprecated. """
        return self.add_array(name,
                              shape,
                              dtype,
                              storage=storage,
                              transient=True,
                              strides=strides,
                              offset=offset,
                              lifetime=lifetime,
                              debuginfo=debuginfo,
                              total_size=total_size,
                              alignment=alignment)

    def fill_scope_connectors(self):
        """ Creates new "IN_%d" and "OUT_%d" connectors on each scope entry
            and exit, depending on array names. """
        for nid, node in enumerate(self.nodes()):
            ####################################################
            # Add connectors to scope entries
            if isinstance(node, nd.EntryNode):
                # Find current number of input connectors
                num_inputs = len(
                    [e for e in self.in_edges(node) if e.dst_conn is not None and e.dst_conn.startswith("IN_")])

                conn_to_data = {}

                # Append input connectors and get mapping of connectors to data
                for edge in self.in_edges(node):
                    if edge.data.data in conn_to_data:
                        raise NotImplementedError(
                            f"Cannot fill scope connectors in SDFGState {self.label} because EntryNode {node.label} "
                            f"has multiple input edges from data {edge.data.data}.")
                    # We're only interested in edges without connectors
                    if edge.dst_conn is not None or edge.data.data is None:
                        continue
                    edge._dst_conn = "IN_" + str(num_inputs + 1)
                    node.add_in_connector(edge.dst_conn)
                    conn_to_data[edge.data.data] = num_inputs + 1

                    num_inputs += 1

                # Set the corresponding output connectors
                for edge in self.out_edges(node):
                    if edge.src_conn is not None:
                        continue
                    if edge.data.data is None:
                        continue
                    edge._src_conn = "OUT_" + str(conn_to_data[edge.data.data])
                    node.add_out_connector(edge.src_conn)
            ####################################################
            # Same treatment for scope exits
            if isinstance(node, nd.ExitNode):
                # Find current number of output connectors
                num_outputs = len(
                    [e for e in self.out_edges(node) if e.src_conn is not None and e.src_conn.startswith("OUT_")])

                conn_to_data = {}

                # Append output connectors and get mapping of connectors to data
                for edge in self.out_edges(node):
                    if edge.src_conn is not None and edge.src_conn.startswith("OUT_"):
                        conn_to_data[edge.data.data] = edge.src_conn[4:]

                    # We're only interested in edges without connectors
                    if edge.src_conn is not None or edge.data.data is None:
                        continue
                    edge._src_conn = "OUT_" + str(num_outputs + 1)
                    node.add_out_connector(edge.src_conn)
                    conn_to_data[edge.data.data] = num_outputs + 1

                    num_outputs += 1

                # Set the corresponding input connectors
                for edge in self.in_edges(node):
                    if edge.dst_conn is not None:
                        continue
                    if edge.data.data is None:
                        continue
                    edge._dst_conn = "IN_" + str(conn_to_data[edge.data.data])
                    node.add_in_connector(edge.dst_conn)


@make_properties
class ContinueBlock(ControlFlowBlock):
    """ Special control flow block to represent a continue inside of loops. """

    def __repr__(self):
        return f'ContinueBlock ({self.label})'

    def to_json(self, parent=None):
        tmp = super().to_json(parent)
        tmp['nodes'] = []
        tmp['edges'] = []
        return tmp


@make_properties
class BreakBlock(ControlFlowBlock):
    """ Special control flow block to represent a continue inside of loops or switch / select blocks. """

    def __repr__(self):
        return f'BreakBlock ({self.label})'

    def to_json(self, parent=None):
        tmp = super().to_json(parent)
        tmp['nodes'] = []
        tmp['edges'] = []
        return tmp


@make_properties
class ReturnBlock(ControlFlowBlock):
    """ Special control flow block to represent an early return out of the SDFG or a nested procedure / SDFG. """

    def __repr__(self):
        return f'ReturnBlock ({self.label})'

    def to_json(self, parent=None):
        tmp = super().to_json(parent)
        tmp['nodes'] = []
        tmp['edges'] = []
        return tmp


class StateSubgraphView(SubgraphView, DataflowGraphView):
    """ A read-only subgraph view of an SDFG state. """

    def __init__(self, graph, subgraph_nodes):
        super().__init__(graph, subgraph_nodes)

    @property
    def sdfg(self) -> 'SDFG':
        state: SDFGState = self.graph
        return state.sdfg


@make_properties
class ControlFlowRegion(OrderedDiGraph[ControlFlowBlock, 'dace.sdfg.InterstateEdge'], ControlGraphView,
                        ControlFlowBlock):

    def __init__(self, label: str = '', sdfg: Optional['SDFG'] = None):
        OrderedDiGraph.__init__(self)
        ControlGraphView.__init__(self)
        ControlFlowBlock.__init__(self, label, sdfg)

        self._labels: Set[str] = set()
        self._start_block: Optional[int] = None
        self._cached_start_block: Optional[ControlFlowBlock] = None
        self._cfg_list: List['ControlFlowRegion'] = [self]

    @property
    def root_sdfg(self) -> 'SDFG':
        from dace.sdfg.sdfg import SDFG  # Avoid import loop
        if not isinstance(self.cfg_list[0], SDFG):
            raise RuntimeError('Root CFG is not of type SDFG')
        return self.cfg_list[0]

    def reset_cfg_list(self) -> List['ControlFlowRegion']:
        """
        Reset the CFG list when changes have been made to the SDFG's CFG tree.
        This collects all control flow graphs recursively and propagates the collection to all CFGs as the new CFG list.

        :return: The newly updated CFG list.
        """
        if isinstance(self, dace.SDFG) and self.parent_sdfg is not None:
            return self.parent_sdfg.reset_cfg_list()
        elif self._parent_graph is not None:
            return self._parent_graph.reset_cfg_list()
        else:
            # Propagate new CFG list to all children
            all_cfgs = list(self.all_control_flow_regions(recursive=True))
            for g in all_cfgs:
                g._cfg_list = all_cfgs
        return self._cfg_list

    def update_cfg_list(self, cfg_list):
        """
        Given a collection of CFGs, add them all to the current SDFG's CFG list.
        Any CFGs already in the list are skipped, and the newly updated list is propagated across all CFGs in the CFG
        tree.

        :param cfg_list: The collection of CFGs to add to the CFG list.
        """
        # TODO: Refactor
        sub_cfg_list = self._cfg_list
        for g in cfg_list:
            if g not in sub_cfg_list:
                sub_cfg_list.append(g)
        ptarget = None
        if isinstance(self, dace.SDFG) and self.parent_sdfg is not None:
            ptarget = self.parent_sdfg
        elif self._parent_graph is not None:
            ptarget = self._parent_graph
        if ptarget is not None:
            ptarget.update_cfg_list(sub_cfg_list)
            self._cfg_list = ptarget.cfg_list
            for g in sub_cfg_list:
                g._cfg_list = self._cfg_list
        else:
            self._cfg_list = sub_cfg_list

    def state(self, state_id: int) -> SDFGState:
        node = self.node(state_id)
        if not isinstance(node, SDFGState):
            raise TypeError(f'The node with id {state_id} is not an SDFGState')
        return node

    def inline(self) -> Tuple[bool, Any]:
        """
        Inlines the control flow region into its parent control flow region (if it exists).

        :return: True if the inlining succeeded, false otherwise.
        """
        parent = self.parent_graph
        if parent:
            end_state = parent.add_state(self.label + '_end')

            # Add all region states and make sure to keep track of all the ones that need to be connected in the end.
            to_connect: Set[SDFGState] = set()
            block_to_state_map: Dict[ControlFlowBlock, SDFGState] = dict()
            for node in self.nodes():
                node.label = self.label + '_' + node.label
                parent.add_node(node, ensure_unique_name=True)
                if isinstance(node, ReturnBlock) and isinstance(parent, dace.SDFG):
                    # If a return block is being inlined into an SDFG, convert it into a regular state. Otherwise it
                    # remains as-is.
                    newnode = parent.add_state(node.label)
                    block_to_state_map[node] = newnode
                elif self.out_degree(node) == 0:
                    to_connect.add(node)

            # Add all region edges.
            for edge in self.edges():
                src = block_to_state_map[edge.src] if edge.src in block_to_state_map else edge.src
                dst = block_to_state_map[edge.dst] if edge.dst in block_to_state_map else edge.dst
                parent.add_edge(src, dst, edge.data)

            # Redirect all edges to the region to the internal start state.
            for b_edge in parent.in_edges(self):
                parent.add_edge(b_edge.src, self.start_block, b_edge.data)
                parent.remove_edge(b_edge)
            # Redirect all edges exiting the region to instead exit the end state.
            for a_edge in parent.out_edges(self):
                parent.add_edge(end_state, a_edge.dst, a_edge.data)
                parent.remove_edge(a_edge)

            for node in to_connect:
                parent.add_edge(node, end_state, dace.InterstateEdge())

            # Remove the original control flow region (self) from the parent graph.
            parent.remove_node(self)

            sdfg = parent if isinstance(parent, dace.SDFG) else parent.sdfg
            sdfg.reset_cfg_list()

            return True, end_state

        return False, None

    ###################################################################
    # CFG API methods

    def add_return(self, label=None) -> ReturnBlock:
        label = self._ensure_unique_block_name(label)
        block = ReturnBlock(label)
        self._labels.add(label)
        self.add_node(block)
        return block

    def add_edge(self, src: ControlFlowBlock, dst: ControlFlowBlock, data: 'dace.sdfg.InterstateEdge'):
        """ Adds a new edge to the graph. Must be an InterstateEdge or a subclass thereof.

            :param u: Source node.
            :param v: Destination node.
            :param edge: The edge to add.
        """
        if not isinstance(src, ControlFlowBlock):
            raise TypeError('Expected ControlFlowBlock, got ' + str(type(src)))
        if not isinstance(dst, ControlFlowBlock):
            raise TypeError('Expected ControlFlowBlock, got ' + str(type(dst)))
        if not isinstance(data, dace.sdfg.InterstateEdge):
            raise TypeError('Expected InterstateEdge, got ' + str(type(data)))
        if dst is self._cached_start_block:
            self._cached_start_block = None
        return super().add_edge(src, dst, data)

    def _ensure_unique_block_name(self, proposed: Optional[str] = None) -> str:
        if self._labels is None or len(self._labels) != self.number_of_nodes():
            self._labels = set(s.label for s in self.nodes())
        return dt.find_new_name(proposed or 'block', self._labels)

    def add_node(self,
                 node,
                 is_start_block: bool = False,
                 ensure_unique_name: bool = False,
                 *,
                 is_start_state: bool = None):
        if not isinstance(node, ControlFlowBlock):
            raise TypeError('Expected ControlFlowBlock, got ' + str(type(node)))

        if ensure_unique_name:
            node.label = self._ensure_unique_block_name(node.label)

        super().add_node(node)
        self._cached_start_block = None
        node.parent_graph = self
        if isinstance(self, dace.SDFG):
            node.sdfg = self
        else:
            node.sdfg = self.sdfg
        start_block = is_start_block
        if is_start_state is not None:
            warnings.warn('is_start_state is deprecated, use is_start_block instead', DeprecationWarning)
            start_block = is_start_state

        if start_block:
            self.start_block = len(self.nodes()) - 1
            self._cached_start_block = node

    def add_state(self, label=None, is_start_block=False, *, is_start_state: Optional[bool] = None) -> SDFGState:
        label = self._ensure_unique_block_name(label)
        state = SDFGState(label)
        self._labels.add(label)
        start_block = is_start_block
        if is_start_state is not None:
            warnings.warn('is_start_state is deprecated, use is_start_block instead', DeprecationWarning)
            start_block = is_start_state
        self.add_node(state, is_start_block=start_block)
        return state

    def add_state_before(self,
                         state: SDFGState,
                         label=None,
                         is_start_block=False,
                         condition: Optional[CodeBlock] = None,
                         assignments: Optional[Dict] = None,
                         *,
                         is_start_state: Optional[bool] = None) -> SDFGState:
        """ Adds a new SDFG state before an existing state, reconnecting predecessors to it instead.

            :param state: The state to prepend the new state before.
            :param label: State label.
            :param is_start_block: If True, resets scope block starting state to this state.
            :param condition: Transition condition of the newly created edge between state and the new state.
            :param assignments: Assignments to perform upon transition.
            :return: A new SDFGState object.
        """
        new_state = self.add_state(label, is_start_block=is_start_block, is_start_state=is_start_state)
        # Reconnect
        for e in self.in_edges(state):
            self.remove_edge(e)
            self.add_edge(e.src, new_state, e.data)
        # Add the new edge
        self.add_edge(new_state, state, dace.sdfg.InterstateEdge(condition=condition, assignments=assignments))
        return new_state

    def add_state_after(self,
                        state: SDFGState,
                        label=None,
                        is_start_block=False,
                        condition: Optional[CodeBlock] = None,
                        assignments: Optional[Dict] = None,
                        *,
                        is_start_state: Optional[bool] = None) -> SDFGState:
        """ Adds a new SDFG state after an existing state, reconnecting it to the successors instead.

            :param state: The state to append the new state after.
            :param label: State label.
            :param is_start_block: If True, resets scope block starting state to this state.
            :param condition: Transition condition of the newly created edge between state and the new state.
            :param assignments: Assignments to perform upon transition.
            :return: A new SDFGState object.
        """
        new_state = self.add_state(label, is_start_block=is_start_block, is_start_state=is_start_state)
        # Reconnect
        for e in self.out_edges(state):
            self.remove_edge(e)
            self.add_edge(new_state, e.dst, e.data)
        # Add the new edge
        self.add_edge(state, new_state, dace.sdfg.InterstateEdge(condition=condition, assignments=assignments))
        return new_state

    ###################################################################
    # Traversal methods

    def all_control_flow_regions(self, recursive=False) -> Iterator['ControlFlowRegion']:
        """ Iterate over this and all nested control flow regions. """
        yield self
        for block in self.nodes():
            if isinstance(block, SDFGState) and recursive:
                for node in block.nodes():
                    if isinstance(node, nd.NestedSDFG):
                        yield from node.sdfg.all_control_flow_regions(recursive=recursive)
            elif isinstance(block, ControlFlowRegion):
                yield from block.all_control_flow_regions(recursive=recursive)

    def all_sdfgs_recursive(self) -> Iterator['SDFG']:
        """ Iterate over this and all nested SDFGs. """
        for cfg in self.all_control_flow_regions(recursive=True):
            if isinstance(cfg, dace.SDFG):
                yield cfg

    def all_states(self) -> Iterator[SDFGState]:
        """ Iterate over all states in this control flow graph. """
        for block in self.nodes():
            if isinstance(block, SDFGState):
                yield block
            elif isinstance(block, ControlFlowRegion):
                yield from block.all_states()

    def all_control_flow_blocks(self, recursive=False) -> Iterator[ControlFlowBlock]:
        """ Iterate over all control flow blocks in this control flow graph. """
        for cfg in self.all_control_flow_regions(recursive=recursive):
            for block in cfg.nodes():
                yield block

    def all_interstate_edges(self, recursive=False) -> Iterator[Edge['dace.sdfg.InterstateEdge']]:
        """ Iterate over all interstate edges in this control flow graph. """
        for cfg in self.all_control_flow_regions(recursive=recursive):
            for edge in cfg.edges():
                yield edge

    ###################################################################
    # Inherited / Overrides

    def _used_symbols_internal(self,
                               all_symbols: bool,
                               defined_syms: Optional[Set] = None,
                               free_syms: Optional[Set] = None,
                               used_before_assignment: Optional[Set] = None,
                               keep_defined_in_mapping: bool = False) -> Tuple[Set[str], Set[str], Set[str]]:
        defined_syms = set() if defined_syms is None else defined_syms
        free_syms = set() if free_syms is None else free_syms
        used_before_assignment = set() if used_before_assignment is None else used_before_assignment

        try:
            ordered_blocks = self.bfs_nodes(self.start_block)
        except ValueError:  # Failsafe (e.g., for invalid or empty SDFGs)
            ordered_blocks = self.nodes()

        for block in ordered_blocks:
            state_symbols = set()
            if isinstance(block, ControlFlowRegion):
                b_free_syms, b_defined_syms, b_used_before_syms = block._used_symbols_internal(all_symbols,
                                                                                               defined_syms,
                                                                                               free_syms,
                                                                                               used_before_assignment,
                                                                                               keep_defined_in_mapping)
                free_syms |= b_free_syms
                defined_syms |= b_defined_syms
                used_before_assignment |= b_used_before_syms
                state_symbols = b_free_syms
            else:
                state_symbols = block.used_symbols(all_symbols, keep_defined_in_mapping)
                free_syms |= state_symbols

            # Add free inter-state symbols
            for e in self.out_edges(block):
                # NOTE: First we get the true InterstateEdge free symbols, then we compute the newly defined symbols by
                # subracting the (true) free symbols from the edge's assignment keys. This way we can correctly
                # compute the symbols that are used before being assigned.
                efsyms = e.data.used_symbols(all_symbols)
                # collect symbols representing data containers
                dsyms = {sym for sym in efsyms if sym in self.sdfg.arrays}
                for d in dsyms:
                    efsyms |= {str(sym) for sym in self.sdfg.arrays[d].used_symbols(all_symbols)}
                defined_syms |= set(e.data.assignments.keys()) - (efsyms | state_symbols)
                used_before_assignment.update(efsyms - defined_syms)
                free_syms |= efsyms

        # Remove symbols that were used before they were assigned.
        defined_syms -= used_before_assignment

        if isinstance(self, dace.SDFG):
            # Remove from defined symbols those that are in the symbol mapping
            if self.parent_nsdfg_node is not None and keep_defined_in_mapping:
                defined_syms -= set(self.parent_nsdfg_node.symbol_mapping.keys())

            # Add the set of SDFG symbol parameters
            # If all_symbols is False, those symbols would only be added in the case of non-Python tasklets
            if all_symbols:
                free_syms |= set(self.symbols.keys())

        # Subtract symbols defined in inter-state edges and constants from the list of free symbols.
        free_syms -= defined_syms

        return free_syms, defined_syms, used_before_assignment

    def to_json(self, parent=None):
        graph_json = OrderedDiGraph.to_json(self)
        block_json = ControlFlowBlock.to_json(self, parent)
        graph_json.update(block_json)

        graph_json['cfg_list_id'] = int(self.cfg_id)
        graph_json['start_block'] = self._start_block

        return graph_json

    @classmethod
    def from_json(cls, json_obj, context=None):
        context = context or {'sdfg': None, 'parent_graph': None}
        _type = json_obj['type']
        if _type != cls.__name__:
            raise TypeError("Class type mismatch")

        nodes = json_obj['nodes']
        edges = json_obj['edges']

        ret = cls(label=json_obj['label'], sdfg=context['sdfg'])

        dace.serialize.set_properties_from_json(ret, json_obj)

        nodelist = []
        for n in nodes:
            nci = copy.copy(context)
            nci['parent_graph'] = ret

            block = dace.serialize.from_json(n, context=nci)
            ret.add_node(block)
            nodelist.append(block)

        for e in edges:
            e = dace.serialize.from_json(e)
            ret.add_edge(nodelist[int(e.src)], nodelist[int(e.dst)], e.data)

        if 'start_block' in json_obj:
            ret._start_block = json_obj['start_block']

        return ret

    ###################################################################
    # Getters, setters, and builtins

    def __str__(self):
        return ControlFlowBlock.__str__(self)

    def __repr__(self) -> str:
        return f'{self.__class__.__name__} ({self.label})'

    @property
    def cfg_list(self) -> List['ControlFlowRegion']:
        return self._cfg_list

    @property
    def cfg_id(self) -> int:
        """
        Returns the unique index of the current CFG within the current tree of CFGs (Top-level CFG/SDFG is 0, nested
        CFGs/SDFGs are greater).
        """
        return self.cfg_list.index(self)

    @property
    def start_block(self):
        """ Returns the starting block of this ControlFlowGraph. """
        if self._cached_start_block is not None:
            return self._cached_start_block

        source_nodes = self.source_nodes()
        if len(source_nodes) == 1:
            self._cached_start_block = source_nodes[0]
            return source_nodes[0]
        # If the starting block is ambiguous allow manual override.
        if self._start_block is not None:
            self._cached_start_block = self.node(self._start_block)
            return self._cached_start_block
        raise ValueError('Ambiguous or undefined starting block for ControlFlowGraph, '
                         'please use "is_start_block=True" when adding the '
                         'starting block with "add_state" or "add_node"')

    @start_block.setter
    def start_block(self, block_id):
        """ Manually sets the starting block of this ControlFlowGraph.

            :param block_id: The node ID (use `node_id(block)`) of the block to set.
        """
        if block_id < 0 or block_id >= self.number_of_nodes():
            raise ValueError('Invalid state ID')
        self._start_block = block_id
        self._cached_start_block = self.node(block_id)


@make_properties
class LoopRegion(ControlFlowRegion):
    """
    A control flow region that represents a loop.

    Like in traditional programming languages, a loop has a condition that is checked before each iteration.
    It may have zero or more initialization statements that are executed before the first loop iteration, and zero or
    more update statements that are executed after each iteration. For example, a loop with only a condition and neither
    an initialization nor an update statement is equivalent to a while loop, while a loop with initialization and update
    statements represents a for loop. Loops may additionally be inverted, meaning that the condition is checked after
    the first iteration instead of before.

    A loop region, like any other control flow region, has a single distinct entry / start block, and one or more
    exit blocks. Exit blocks are blocks that have no outgoing edges or only conditional outgoing edges. Whenever an
    exit block finshes executing, one iteration of the loop is completed.

    Loops may have an arbitrary number of break states. Whenever a break state finishes executing, the loop is exited
    immediately. A loop may additionally have an arbitrary number of continue states. Whenever a continue state finishes
    executing, the next iteration of the loop is started immediately (with execution of the update statement(s), if
    present).
    """

    update_statement = CodeProperty(optional=True,
                                    allow_none=True,
                                    default=None,
                                    desc='The loop update statement. May be None if the update happens elsewhere.')
    init_statement = CodeProperty(optional=True,
                                  allow_none=True,
                                  default=None,
                                  desc='The loop init statement. May be None if the initialization happens elsewhere.')
    loop_condition = CodeProperty(allow_none=True, default=None, desc='The loop condition')
    inverted = Property(dtype=bool,
                        default=False,
                        desc='If True, the loop condition is checked after the first iteration.')
    loop_variable = Property(dtype=str, default='', desc='The loop variable, if given')

    def __init__(self,
                 label: str,
                 condition_expr: Optional[str] = None,
                 loop_var: Optional[str] = None,
                 initialize_expr: Optional[str] = None,
                 update_expr: Optional[str] = None,
                 inverted: bool = False,
                 sdfg: Optional['SDFG'] = None):
        super(LoopRegion, self).__init__(label, sdfg)

        if initialize_expr is not None:
            self.init_statement = CodeBlock(initialize_expr)
        else:
            self.init_statement = None

        if condition_expr:
            self.loop_condition = CodeBlock(condition_expr)
        else:
            self.loop_condition = CodeBlock('True')

        if update_expr is not None:
            self.update_statement = CodeBlock(update_expr)
        else:
            self.update_statement = None

        self.loop_variable = loop_var or ''
        self.inverted = inverted

    def inline(self) -> Tuple[bool, Any]:
        """
        Inlines the loop region into its parent control flow region.

        :return: True if the inlining succeeded, false otherwise.
        """
        parent = self.parent_graph
        if not parent:
            raise RuntimeError('No top-level SDFG present to inline into')

        # Avoid circular imports
        from dace.frontend.python import astutils

        # Check that the loop initialization and update statements each only contain assignments, if the loop has any.
        if self.init_statement is not None:
            if isinstance(self.init_statement.code, list):
                for stmt in self.init_statement.code:
                    if not isinstance(stmt, astutils.ast.Assign):
                        return False, None
        if self.update_statement is not None:
            if isinstance(self.update_statement.code, list):
                for stmt in self.update_statement.code:
                    if not isinstance(stmt, astutils.ast.Assign):
                        return False, None

        # First recursively inline any other contained control flow regions other than loops to ensure break, continue,
        # and return are inlined correctly.
        def recursive_inline_cf_regions(region: ControlFlowRegion) -> None:
            for block in region.nodes():
                if isinstance(block, ControlFlowRegion) and not isinstance(block, LoopRegion):
                    recursive_inline_cf_regions(block)
                    block.inline()
        recursive_inline_cf_regions(self)

        # Add all boilerplate loop states necessary for the structure.
        init_state = parent.add_state(self.label + '_init')
        guard_state = parent.add_state(self.label + '_guard')
        end_state = parent.add_state(self.label + '_end')
        loop_latch_state = parent.add_state(self.label + '_latch')

        # Add all loop states and make sure to keep track of all the ones that need to be connected in the end.
        # Return blocks are inlined as-is. If the parent graph is an SDFG, they are converted to states, otherwise
        # they are left as explicit exit blocks.
        connect_to_latch: Set[SDFGState] = set()
        connect_to_end: Set[SDFGState] = set()
        block_to_state_map: Dict[ControlFlowBlock, SDFGState] = dict()
        for node in self.nodes():
            node.label = self.label + '_' + node.label
            if isinstance(node, BreakBlock):
                newnode = parent.add_state(node.label)
                connect_to_end.add(newnode)
                block_to_state_map[node] = newnode
            elif isinstance(node, ContinueBlock):
                newnode = parent.add_state(node.label)
                connect_to_latch.add(newnode)
                block_to_state_map[node] = newnode
            elif isinstance(node, ReturnBlock) and isinstance(parent, dace.SDFG):
                newnode = parent.add_state(node.label)
                block_to_state_map[node] = newnode
            else:
                if self.out_degree(node) == 0:
                    connect_to_latch.add(node)
                parent.add_node(node, ensure_unique_name=True)

        # Add all internal loop edges.
        for edge in self.edges():
            src = block_to_state_map[edge.src] if edge.src in block_to_state_map else edge.src
            dst = block_to_state_map[edge.dst] if edge.dst in block_to_state_map else edge.dst
            parent.add_edge(src, dst, edge.data)

        # Redirect all edges to the loop to the init state.
        for b_edge in parent.in_edges(self):
            parent.add_edge(b_edge.src, init_state, b_edge.data)
            parent.remove_edge(b_edge)
        # Redirect all edges exiting the loop to instead exit the end state.
        for a_edge in parent.out_edges(self):
            parent.add_edge(end_state, a_edge.dst, a_edge.data)
            parent.remove_edge(a_edge)

        # Add an initialization edge that initializes the loop variable if applicable.
        init_edge = dace.InterstateEdge()
        if self.init_statement is not None:
            init_edge.assignments = {}
            for stmt in self.init_statement.code:
                assign: astutils.ast.Assign = stmt
                init_edge.assignments[assign.targets[0].id] = astutils.unparse(assign.value)
        if self.inverted:
            parent.add_edge(init_state, self.start_block, init_edge)
        else:
            parent.add_edge(init_state, guard_state, init_edge)

        # Connect the loop tail.
        update_edge = dace.InterstateEdge()
        if self.update_statement is not None:
            update_edge.assignments = {}
            for stmt in self.update_statement.code:
                assign: astutils.ast.Assign = stmt
                update_edge.assignments[assign.targets[0].id] = astutils.unparse(assign.value)
        parent.add_edge(loop_latch_state, guard_state, update_edge)

        # Add condition checking edges and connect the guard state.
        cond_expr = self.loop_condition.code
        parent.add_edge(guard_state, end_state,
                        dace.InterstateEdge(CodeBlock(astutils.negate_expr(cond_expr)).code))
        parent.add_edge(guard_state, self.start_block, dace.InterstateEdge(CodeBlock(cond_expr).code))

        # Connect any end states from the loop's internal state machine to the tail state so they end a
        # loop iteration. Do the same for any continue states, and connect any break states to the end of the loop.
        for node in connect_to_latch:
            parent.add_edge(node, loop_latch_state, dace.InterstateEdge())
        for node in connect_to_end:
            parent.add_edge(node, end_state, dace.InterstateEdge())

        parent.remove_node(self)

        sdfg = parent if isinstance(parent, dace.SDFG) else parent.sdfg
        sdfg.reset_cfg_list()

        return True, (init_state, guard_state, end_state)

    def _used_symbols_internal(self,
                               all_symbols: bool,
                               defined_syms: Optional[Set] = None,
                               free_syms: Optional[Set] = None,
                               used_before_assignment: Optional[Set] = None,
                               keep_defined_in_mapping: bool = False) -> Tuple[Set[str], Set[str], Set[str]]:
        defined_syms = set() if defined_syms is None else defined_syms
        free_syms = set() if free_syms is None else free_syms
        used_before_assignment = set() if used_before_assignment is None else used_before_assignment

        defined_syms.add(self.loop_variable)
        if self.init_statement is not None:
            free_syms |= self.init_statement.get_free_symbols()
        if self.update_statement is not None:
            free_syms |= self.update_statement.get_free_symbols()
        free_syms |= self.loop_condition.get_free_symbols()

        b_free_symbols, b_defined_symbols, b_used_before_assignment = super()._used_symbols_internal(
            all_symbols, keep_defined_in_mapping=keep_defined_in_mapping)
        outside_defined = defined_syms - used_before_assignment
        used_before_assignment |= ((b_used_before_assignment - {self.loop_variable}) - outside_defined)
        free_syms |= b_free_symbols
        defined_syms |= b_defined_symbols

        defined_syms -= used_before_assignment
        free_syms -= defined_syms

        return free_syms, defined_syms, used_before_assignment

    def replace_dict(self,
                     repl: Dict[str, str],
                     symrepl: Optional[Dict[symbolic.SymbolicType, symbolic.SymbolicType]] = None,
                     replace_in_graph: bool = True,
                     replace_keys: bool = True):
        if replace_keys:
            from dace.sdfg.replace import replace_properties_dict
            replace_properties_dict(self, repl, symrepl)

            if self.loop_variable and self.loop_variable in repl:
                self.loop_variable = repl[self.loop_variable]

        super().replace_dict(repl, symrepl, replace_in_graph)

    def add_break(self, label=None) -> BreakBlock:
        label = self._ensure_unique_block_name(label)
        block = BreakBlock(label)
        self._labels.add(label)
        self.add_node(block)
        return block

    def add_continue(self, label=None) -> ContinueBlock:
        label = self._ensure_unique_block_name(label)
        block = ContinueBlock(label)
        self._labels.add(label)
        self.add_node(block)
        return block

    @property
    def has_continue(self) -> bool:
        for node, _ in self.all_nodes_recursive(lambda n, _: not isinstance(n, (LoopRegion, SDFGState))):
            if isinstance(node, ContinueBlock):
                return True
        return False

    @property
    def has_break(self) -> bool:
        for node, _ in self.all_nodes_recursive(lambda n, _: not isinstance(n, (LoopRegion, SDFGState))):
            if isinstance(node, BreakBlock):
                return True
        return False

    @property
    def has_return(self) -> bool:
        for node, _ in self.all_nodes_recursive(lambda n, _: not isinstance(n, (LoopRegion, SDFGState))):
            if isinstance(node, ReturnBlock):
                return True
        return False

@make_properties
class UserRegion(ControlFlowRegion):
    debuginfo = DebugInfoProperty()
    def __init__(self, label: str, sdfg: Optional['SDFG']=None, debuginfo: Optional[dtypes.DebugInfo]=None):
        super().__init__(label, sdfg)
        self.debuginfo = debuginfo

@make_properties
class FunctionCallRegion(ControlFlowRegion):
    arguments = DictProperty(str, str)
    def __init__(self, label: str, arguments: Dict[str, str] = {}, sdfg: 'SDFG' = None):
        super().__init__(label, sdfg)
        self.arguments = arguments
