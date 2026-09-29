import os
import random
import time
from google import genai
from google.genai import errors
from neo4j import GraphDatabase
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# --- 1. GEMINI API SETUP ---
api_key = os.getenv("MY_API_KEY")
client = genai.Client(api_key=api_key)

# Pick the model explicitly (override with GEMINI_MODEL in .env). Auto-detecting
# "the first flash model" is unreliable: the API list order is arbitrary and
# includes retired models.
selected_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# --- 2. NEO4J CONNECTION SETUP ---
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password123")

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

# --- 3. FETCH CONTEXT FROM GRAPH MEMORY ---
def get_code_context():
    """Retrieves all function nodes and their raw code from Neo4j."""
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
            context_string += f"{record['code']}\n"
    return context_string

# --- RETRY WITH BACKOFF FOR TRANSIENT GEMINI ERRORS (503 overloaded, 429 rate limit) ---
RETRYABLE_CODES = {429, 500, 503, 504}

def ask_with_retry(prompt, max_attempts=6, base_delay=2.0, max_delay=60.0):
    for attempt in range(1, max_attempts + 1):
        try:
            chat = client.chats.create(model=selected_model)
            return chat.send_message(prompt)
        except errors.APIError as e:
            if e.code not in RETRYABLE_CODES or attempt == max_attempts:
                raise
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * random.uniform(0.5, 1.0)
            print(f"  Gemini returned {e.code}; retry {attempt}/{max_attempts - 1} in {delay:.1f}s...")
            time.sleep(delay)

# --- 4. THE AGENT LOGIC ---
if __name__ == "__main__":
    print("Connecting to Graph Memory...")
    print(f"AI Brain Selected: {selected_model}")
    
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
    print("\nThinking (Asking Gemini)...\n")
    
    try:
        # Send context + question to Gemini
        full_prompt = system_prompt + "\n\nUser Question:\n" + user_query
        response = ask_with_retry(full_prompt)
        print("--- AGENT RESPONSE ---")
        print(response.text)
    except Exception as e:
        print(f"Error communicating with Gemini: {e}")
    
    driver.close()