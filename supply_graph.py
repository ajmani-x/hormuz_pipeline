"""
Supply Chain Graph
------------------
A small directed graph over the reference network (suppliers, shipping
routes, refineries, SPR sites), built from the static data in
data_sources.py. Pure stdlib, no networkx — but it supports the kind of
multi-hop query a flat lookup table can't answer directly, e.g. "which
suppliers and reserve sites are exposed by a disrupted route", not just
"which refineries".

Edge types:
  supplier  --ships_via-->          route
  route     --feeds-->              refinery
  supplier  --currently_supplies--> refinery
  refinery  --backed_by-->          spr_site
"""
from collections import defaultdict

from data_sources import REFINERIES, SUPPLIERS, SPR_SITES


class SupplyGraph:
    def __init__(self):
        self.node_type = {}                 # node_id -> type
        self._out = defaultdict(list)       # node_id -> [(neighbor, edge_type)]
        self._in = defaultdict(list)

    def add_node(self, node_id: str, node_type: str):
        self.node_type[node_id] = node_type

    def add_edge(self, src: str, dst: str, edge_type: str):
        self._out[src].append((dst, edge_type))
        self._in[dst].append((src, edge_type))

    def successors(self, node_id: str, edge_type: str = None) -> list:
        return [n for n, t in self._out.get(node_id, []) if edge_type is None or t == edge_type]

    def predecessors(self, node_id: str, edge_type: str = None) -> list:
        return [n for n, t in self._in.get(node_id, []) if edge_type is None or t == edge_type]

    def nodes_of_type(self, node_type: str) -> list:
        return [n for n, t in self.node_type.items() if t == node_type]

    def reachable(self, start_nodes: list, node_type: str = None) -> list:
        """BFS forward from start_nodes along outgoing edges. Optionally
        filter the result down to a single node type."""
        seen = set(start_nodes)
        queue = list(start_nodes)
        while queue:
            current = queue.pop(0)
            for neighbor in self.successors(current):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append(neighbor)
        seen -= set(start_nodes)
        if node_type:
            return [n for n in seen if self.node_type.get(n) == node_type]
        return list(seen)


def build_supply_graph() -> SupplyGraph:
    g = SupplyGraph()

    routes = {r.primary_route for r in REFINERIES} | {s.route for s in SUPPLIERS}
    for route in routes:
        g.add_node(route, "route")

    for s in SUPPLIERS:
        g.add_node(s.name, "supplier")
        g.add_edge(s.name, s.route, "ships_via")

    for r in REFINERIES:
        g.add_node(r.name, "refinery")
        g.add_edge(r.primary_route, r.name, "feeds")
        for supplier_name in r.current_suppliers:
            g.add_edge(supplier_name, r.name, "currently_supplies")

    for site in SPR_SITES:
        g.add_node(site.name, "spr_site")
        for refinery_name in site.serves_refineries:
            g.add_edge(refinery_name, site.name, "backed_by")

    return g


SUPPLY_GRAPH = build_supply_graph()
