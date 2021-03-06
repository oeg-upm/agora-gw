"""
#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=#
  Ontology Engineering Group
        http://www.oeg-upm.net/
#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=#
  Copyright (C) 2017 Ontology Engineering Group.
#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=#
  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

            http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
#-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=#
"""
import logging
from collections import defaultdict

from agora.engine.plan import AGP, TP, find_root_types
from agora.graph.evaluate import traverse_part
from rdflib import RDF, Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.sparql import Query

from agora_gw.data import R
from agora_gw.ecosystem.description import build_component, build_TED

__author__ = 'Fernando Serena'

log = logging.getLogger('agora.gateway.discover')


def extract_bgps(q, cache=None):
    if cache is not None and q in cache:
        return cache[q]

    if not isinstance(q, Query):
        parsetree = parseQuery(q)
        query = translateQuery(parsetree, None, {})
    else:
        query = q

    part = query.algebra
    filters = {}
    bgps = []

    for p in traverse_part(part, filters):
        bgps.append(p)

    if cache is not None:
        cache[q] = bgps, filters
    return bgps, filters


def build_agp(bgp):
    agp = AGP([TP(*tp) for tp in bgp.triples], prefixes=R.agora.fountain.prefixes)
    return agp


def bgp_root_types(bgp):
    agp = build_agp(bgp)
    graph = agp.graph
    roots = agp.roots
    str_roots = map(lambda x: str(x), roots)
    root_types = set()
    for c in graph.contexts():
        try:
            root_tps = filter(lambda (s, pr, o): str(s).replace('?', '') in str_roots, c.triples((None, None, None)))
            context_root_types = find_root_types(R.agora.fountain, root_tps, c, extend=False).values()
            root_types.update(reduce(lambda x, y: x.union(y), context_root_types, set()))
        except TypeError:
            pass

    return root_types


def query_root_types(q, bgp_cache=None):
    types = reduce(lambda x, y: x.union(bgp_root_types(y)), extract_bgps(q, cache=bgp_cache)[0], set())
    desc_types = describe_types(types)
    return keep_general(desc_types)


def keep_general(types):
    ids = set(types.keys())
    filtered_ids = filter(lambda x: not set.intersection(set(types[x]['super']), ids), types)
    return {fid: types[fid] for fid in filtered_ids}


def keep_specific(types):
    ids = set(types.keys())
    filtered_ids = filter(lambda x: (types[x]['super'] and not set.intersection(set(types[x]['super']), ids))
                                    or not set.intersection(set(types[x]['sub']), ids), types)
    return {fid: types[fid] for fid in filtered_ids}


def describe_type(t):
    desc = R.agora.fountain.get_type(t)
    desc['id'] = t
    return desc


def describe_types(types):
    return {t: describe_type(t) for t in types}


def is_subclass_of_thing(t):
    return t['id'] == 'wot:Thing' or 'wot:Thing' in t['super']


def tuple_from_result_row(row):
    return tuple([row[var]['value'] for var in row])


def generate_dict(res):
    ns = R.ns()
    d = defaultdict(set)
    for k, v in map(lambda x: tuple_from_result_row(x), res):
        d[k].add(R.n3(v, ns))
    return d


def contains_solutions(id, query, bgp_cache=None):
    result = True
    queries = list(transform_into_specific_queries(id, query, bgp_cache=bgp_cache))
    for sub_query in queries:
        result = result and bool(map(lambda r: r, R.query(sub_query, cache=True, expire=300)))
        if not result:
            break
    return result


def is_target_reachable(source_types, target, fountain=None, cache=None):
    for st in source_types:
        if cache and (st, target) in cache:
            return cache[(st, target)]

        connected = R.link_path(st, target, fountain=fountain)
        if cache is not None:
            cache[(st, target)] = connected
        if connected:
            return True

    return False


def search_things(type, q, fountain, reachability=True, reachability_cache=None, bgp_cache=None):
    res = R.query("""
       prefix core: <http://iot.linkeddata.es/def/core#>
       prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> 
       SELECT DISTINCT * WHERE {
           {
              [] a core:Ecosystem ;
                 core:hasComponent ?s
           }
           UNION
           {
              [] a core:ThingDescription ;
                 core:describes ?s
           }
           ?s a ?type             
           FILTER(isURI(?type) && isURI(?s) && ?type != rdfs:Resource)
       }
       """, cache=True, expire=300, infer=True)

    rd = generate_dict(res)
    type_n3 = type['id']
    all_types = fountain.types

    if reachability_cache is None:
        reachability_cache = {}

    for seed, type_ids in rd.items():
        try:
            types = {t: fountain.get_type(t) for t in type_ids if t in all_types}
            if types and (type_n3 in types or is_target_reachable(types.keys(), type_n3, fountain=fountain,
                                                                  cache=reachability_cache)):
                rd[seed] = types
            else:
                del rd[seed]
        except TypeError:
            del rd[seed]

    final_rd = {}
    for seed in rd:
        if reachability or contains_solutions(seed, q, bgp_cache=bgp_cache):
            final_rd[seed] = rd[seed]

    return final_rd


def make_up_bgp_query(q, predicate_mask, bgp_cache=None):
    bgps, filters = extract_bgps(q, cache=bgp_cache)
    for bgp in bgps:
        desc_tps = filter(lambda tp: tp[1] in predicate_mask, bgp.triples)
        bgp_vars = filter(lambda part: isinstance(part, Variable),
                          reduce(lambda x, y: x.union(list(y)), bgp.triples, set()))

        bgp_filters = reduce(lambda x, y: x.union(filters[v]), [v for v in bgp_vars if v in filters], set())
        filter_clause = 'FILTER(%s)' % ' && '.join(bgp_filters) if bgp_filters else ''

        if desc_tps:
            tps_str = ' .\n'.join([str(TP(*tp)) for tp in desc_tps])
            yield tps_str, filter_clause


def transform_into_specific_queries(id, q, bgp_cache=None):
    desc_predicates = R.thing_describing_predicates(id)
    if RDF.type in desc_predicates:
        desc_predicates.remove(RDF.type)

    td_q = """SELECT DISTINCT * WHERE { GRAPH <%s> { %s  %s } }"""
    for tps_str, filter_clause in make_up_bgp_query(q, desc_predicates, bgp_cache=bgp_cache):
        yield td_q % (id, tps_str, filter_clause)


def transform_into_graph_td_queries(q, bgp_cache=None):
    desc_predicates = R.describing_predicates
    if RDF.type in desc_predicates:
        desc_predicates.remove(RDF.type)

    td_q = """SELECT DISTINCT ?g { GRAPH ?g { %s  %s } }"""
    for tps_str, filter_clause in make_up_bgp_query(q, desc_predicates, bgp_cache=bgp_cache):
        yield td_q % (tps_str, filter_clause)


def discover_ecosystem(q, reachability=False):
    bgp_cache = {}

    # 1. Get all BPG root types
    root_types = query_root_types(q, bgp_cache=bgp_cache)

    if not root_types:
        raise AttributeError('Could not understand the given query')

    log.debug('Triggered discovery for \n{}'.format(q))
    log.debug('Query root types: {}'.format(root_types.keys()))

    # 2. Find relevant things for identified root types
    log.debug('Searching for relevant things...')
    fountain = R.fountain

    reachability_cache = {}
    typed_things = {
        type['id']: search_things(type, q, fountain,
                                  reachability=reachability,
                                  reachability_cache=reachability_cache,
                                  bgp_cache=bgp_cache) for type in root_types.values()}
    log.debug('Found things of different types: {}'.format(typed_things.keys()))

    # 2b. Filter seeds
    log.debug('Analyzing relevant things...')
    graph_td_queries = list(transform_into_graph_td_queries(q, bgp_cache=bgp_cache))
    query_matching_things = set()
    for q in graph_td_queries:
        graphs = map(lambda r: r['g']['value'], R.query(q, cache=True, expire=300))
        query_matching_things.update(set(graphs))

    root_thing_ids = reduce(lambda x, y: x.union(y), typed_things.values(), set())
    root_things = root_thing_ids
    if graph_td_queries:
        root_things = set.intersection(query_matching_things, root_thing_ids)

    log.debug('Discovered {} root things!'.format(len(root_things)))

    # 3. Retrieve/Build ecosystem TDs
    log.debug('Preparing TDs for the discovered ecosystem...')
    node_map = {}
    components = {root: list(build_component(root, node_map=node_map)) for root in root_things}

    # 4. Compose ecosystem description
    log.debug('Building TED of the discovered ecosystem...')
    ted = build_TED(components.values())
    return ted
