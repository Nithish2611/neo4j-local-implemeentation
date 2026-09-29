from neo4j import GraphDatabase
from openai import OpenAI

# --- 1. OLLAMA API SETUP (100% LOCAL & FREE) ---
# Connecting to Ollama's local server instead of cloud
client = OpenAI(
    base_url='http://localhost:11434/v1',
    api_key='ollama',  # API key is required by the library but ignored by Ollama
)
MODEL = "qwen2.5-coder:7b"

# --- 2. NEO4J CONNECTION SETUP ---
NEO4J_URI = "bolt://127.0.0.1:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password123"

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# --- 3. FETCH CONTEXT FROM GRAPH MEMORY ---
def get_code_context():
    """Retrieves all function nodes, their raw code and CALLS edges from Neo4j."""
    query = """
    MATCH (fn:Function)-[:DEFINED_IN]->(f:File)
    OPTIONAL MATCH (fn)-[:CALLS]->(callee:Function)
    RETURN f.name AS file_name, fn.name AS function_name, fn.raw_code AS code,
           collect(DISTINCT callee.name) AS calls
    """
    context_string = ""
    with driver.session() as session:
        result = session.run(query)
        for record in result:
            context_string += f"\n--- File: {record['file_name']} | Function: {record['function_name']} ---\n"
            if record['calls']:
                context_string += f"(calls: {', '.join(record['calls'])})\n"
            context_string += f"{record['code']}\n"
    return context_string

# --- 4. THE AGENT LOGIC ---
if __name__ == "__main__":
    print("Connecting to Graph Memory...")
    graph_context = get_code_context()

    # The exact architectural question
    user_query = "Explain how the calculate_refund and process_payment functions work together. What happens if the amount is negative?"

    # System prompt locking the AI to ONLY use our Graph data
    system_prompt = f"""You are a Senior Python Developer and Knowledge Graph Agent.
    Answer the user's question STRICTLY based on the following code context retrieved from our graph database.
    If the answer is not in the context, say 'I don't have enough information in the graph.'

    CODE CONTEXT:
    {graph_context}
    """

    print(f"\nUser Query: {user_query}")
    print("\nThinking (Asking Local Qwen 2.5 Coder)...")
    print("(This might take 10-20 seconds as it runs completely on your CPU)\n")

    try:
        # Send context + question to local Ollama
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_query}
            ],
            temperature=0.1
        )
        print("--- AGENT RESPONSE ---")
        print(response.choices[0].message.content)
    except Exception as e:
        print(f"Error communicating with Ollama: {e}")
        print("Please make sure the Ollama app is running on your computer.")

    driver.close()
