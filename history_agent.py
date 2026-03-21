import os
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set in .env")

supabase: Client = create_client(url, key)
client = OpenAI()


def find_similar_shipments(origin: str, destination: str, mode: str, commodity_desc: str, limit: int = 3):
    response = client.embeddings.create(
        input=commodity_desc,
        model="text-embedding-3-small"
    )
    query_embedding = response.data[0].embedding

    rpc_params = {
        "p_origin": origin,
        "p_destination": destination,
        "p_mode": mode,
        "p_embedding": query_embedding,
        "match_count": limit
    }

    try:
        result = supabase.rpc('match_shipments', rpc_params).execute()
        return result.data
    except Exception:
        return []


if __name__ == "__main__":
    matches = find_similar_shipments(
        origin="Hamburg port",
        destination="Mumbai",
        mode="sea_freight",
        commodity_desc="spare automotive parts"
    )
    if not matches:
        print("No historical shipments found.")
    else:
        print("Top matches:")
        for i, m in enumerate(matches, 1):
            print(f"  [{i}] {m['commodity']} | Agent: {m['agent_used']} | Rate: ${m['rate_paid']} | {m['transit_time_days']}d | Sim: {m['similarity']:.3f}")
