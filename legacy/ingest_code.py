import tree_sitter_python as tspython
from tree_sitter import Language, Parser
from neo4j import GraphDatabase

# --- 1. NEO4J CONNECTION SETUP ---
NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

# Connect to the local Neo4j Graph Database
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# --- 2. TREE-SITTER PARSER SETUP ---
# Initialize the Python language parser
PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

# --- 3. ZERO-LOSS AST EXTRACTION ---
def extract_calls(node, source_bytes):
    """Collects the names of everything called inside a function body.

    foo()      -> "foo"
    self.foo() -> "foo"   (best effort: attribute calls are matched by method name)
    Nested function definitions are skipped; they get their own CALLS edges.
    """
    calls = set()
    for child in node.children:
        if child.type == 'function_definition':
            continue
        if child.type == 'call':
            target = child.child_by_field_name('function')
            if target is not None and target.type == 'attribute':
                target = target.child_by_field_name('attribute')
            if target is not None and target.type == 'identifier':
                calls.add(source_bytes[target.start_byte:target.end_byte].decode('utf8'))
        calls |= extract_calls(child, source_bytes)
    return calls

def extract_functions(node, source_bytes):
    """Recursively parses the AST to extract function names and exact raw code."""
    functions = []

    # If the node is a python function definition
    if node.type == 'function_definition':
        name_node = node.child_by_field_name('name')
        if name_node:
            func_name = source_bytes[name_node.start_byte:name_node.end_byte].decode('utf8')
            exact_code = source_bytes[node.start_byte:node.end_byte].decode('utf8')

            functions.append({
                "name": func_name,
                "code": exact_code,
                "calls": sorted(extract_calls(node, source_bytes))
            })

    # Recursively check children
    for child in node.children:
        functions.extend(extract_functions(child, source_bytes))

    return functions

# --- 4. GRAPH INGESTION (CYPHER QUERIES) ---
def ingest_into_neo4j(tx, file_name, functions_list):
    """Executes Cypher queries to create File nodes, Function nodes, and links them."""

    # Create the File Node
    tx.run("MERGE (f:File {name: $file_name})", file_name=file_name)

    # Create Function Nodes and link them to the File
    for func in functions_list:
        query = """
        MATCH (f:File {name: $file_name})
        MERGE (fn:Function {name: $func_name})
        SET fn.raw_code = $code
        MERGE (fn)-[:DEFINED_IN]->(f)
        """
        tx.run(query, file_name=file_name, func_name=func['name'], code=func['code'])

    # Link callers to callees. Done after all Function nodes exist so call order in
    # the file does not matter. Calls to anything not in the graph (print, len,
    # library functions) simply match nothing and are skipped.
    for func in functions_list:
        tx.run("""
        MATCH (caller:Function {name: $caller})
        UNWIND $callees AS callee_name
        MATCH (callee:Function {name: callee_name})
        MERGE (caller)-[:CALLS]->(callee)
        """, caller=func['name'], callees=func['calls'])

# --- 5. RUN THE TEST PIPELINE ---
if __name__ == "__main__":
    print("Parsing sample code...")

    # A sample code block to test our memory engine
    sample_code = """
def calculate_refund(amount):
    if amount > 0:
        return amount * 0.9
    return 0

def process_payment(user_id, amount):
    refund_amount = calculate_refund(amount)
    print(f"User {user_id} refunded {refund_amount}")
"""

    # Parse the code into AST
    source_bytes = bytes(sample_code, "utf8")
    tree = parser.parse(source_bytes)

    # Extract nodes
    extracted_funcs = extract_functions(tree.root_node, source_bytes)
    print(f"Extracted {len(extracted_funcs)} functions: {[f['name'] for f in extracted_funcs]}")
    for f in extracted_funcs:
        print(f"  {f['name']} calls {f['calls']}")

    # Push to Neo4j Graph
    print("Ingesting into Neo4j Graph Memory...")
    with driver.session() as session:
        session.execute_write(ingest_into_neo4j, "payment_service.py", extracted_funcs)

    print("Success! Open http://localhost:7474 to see your Code Graph.")
    driver.close()
