# Copyright 2019-2021 ETH Zurich and the DaCe authors. All rights reserved.
""" Implements the map-dim shuffle transformation. """

from typing import List
from dace import dtypes
from dace.memlet import Memlet
from dace.sdfg import SDFG
from dace.sdfg import nodes
from dace.sdfg import utils as sdutil
from dace.sdfg.state import SDFGState
from dace.transformation import transformation
from dace.properties import make_properties, ListProperty


@make_properties
class MapDimShuffle(transformation.SingleStateTransformation):
    """ Implements the map-dim shuffle transformation.
    
        MapDimShuffle takes a map and a list of params.
        It reorders the dimensions in the map such that it matches the list.
    """

    map_entry = transformation.PatternNode(nodes.MapEntry)

    # Properties
    parameters = ListProperty(element_type=str, default=None, desc="Desired order of map parameters")

    @classmethod
    def expressions(cls):
        return [sdutil.node_path_graph(cls.map_entry)]

    def can_be_applied(self, graph, expr_index, sdfg, permissive=False):
        map_entry: nodes.MapEntry = self.map_entry
        if self.parameters is None:
            return False
        if len(self.parameters) != len(map_entry.map.params):
            return False
        if set(self.parameters) != set(map_entry.map.params):
            return False
        return True

    def apply(self, graph: SDFGState, sdfg: SDFG):
        map_entry: nodes.MapEntry = self.map_entry
        new_map_order: List[int] = [map_entry.map.params.index(param) for param in self.parameters]
        map_entry.map.range.ranges = [map_entry.map.range.ranges[new_pos] for new_pos in new_map_order]
        map_entry.map.range.tile_sizes = [map_entry.map.range.tile_sizes[new_pos] for new_pos in new_map_order]
        map_entry.map.params = [map_entry.map.params[new_pos] for new_pos in new_map_order]

        if map_entry.map.schedule == dtypes.ScheduleType.CPU_Multicore_Doacross:
            # For doacross maps, we need to make sure all memlets that carry doacross dependencies are also equally
            # permuted.
            scope_subgraph = graph.scope_subgraph(map_entry)
            for e in scope_subgraph.edges():
                memlet: Memlet = e.data
                if memlet.schedule == dtypes.MemletScheduleType.Doacross_Sink:
                    new_offset = [memlet.doacross_dependency_offset[new_pos] for new_pos in new_map_order]
                    memlet.doacross_dependency_offset = new_offset
