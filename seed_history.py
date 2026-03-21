import os
from dotenv import load_dotenv
from supabase import create_client, Client
from openai import OpenAI

load_dotenv()

# Initialize Supabase
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")

if not url or not key:
    print("❌ Supabase API keys not found in .env")
    exit(1)

supabase: Client = create_client(url, key)

# Initialize OpenAI
client = OpenAI()

# Our mock history (deliberately matching the general routes/modes from our test emails)
mock_data = [
    {
        "origin": "Hamburg port",
        "destination": "Mumbai",
        "mode": "sea_freight",
        "weight_kg": 1400,
        "commodity": "automotive engine blocks",
        "agent_used": "Kuehne+Nagel",
        "rate_paid": 2100,
        "transit_time_days": 28
    },
    {
        "origin": "Hamburg port",
        "destination": "Mumbai",
        "mode": "sea_freight",
        "weight_kg": 800,
        "commodity": "rubber tires and suspension parts",
        "agent_used": "DB Schenker",
        "rate_paid": 1200,
        "transit_time_days": 30
    },
    {
        "origin": "Shenzhen Port, China",
        "destination": "Los Angeles, CA",
        "mode": "sea_freight",
        "weight_kg": 4000,
        "commodity": "wireless headphones and chargers",
        "agent_used": "Flexport",
        "rate_paid": 4500,
        "transit_time_days": 21
    },
    {
        "origin": "Frankfurt",
        "destination": "Dubai",
        "mode": "air_freight",
        "weight_kg": 350,
        "commodity": "surgical masks, gloves, and medical gauze",
        "agent_used": "DSV",
        "rate_paid": 1800,
        "transit_time_days": 2
    },
    {
        "origin": "Milan, Italy",
        "destination": "Paris",
        "mode": "road",
        "weight_kg": 850,
        "commodity": "luxury clothing, silk shirts, and leather jackets",
        "agent_used": "Bollore",
        "rate_paid": 950,
        "transit_time_days": 3
    },
    {
        "origin": "Houston, Texas",
        "destination": "Nhava Sheva, India",
        "mode": "sea_freight",
        "weight_kg": 2500,
        "commodity": "oil drilling rigs and industrial piping",
        "agent_used": "Expeditors",
        "rate_paid": 3400,
        "transit_time_days": 40
    }
]

def get_embedding(text):
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small"
    )
    return response.data[0].embedding

if __name__ == "__main__":
    print("🚀 Seeding PostgreSQL database with mock historical shipments...")
    
    # Optional: Clear existing data so we don't duplicate on re-runs
    try:
        supabase.table("shipments").delete().neq("id", 0).execute()
        print("Cleared old data.")
    except Exception as e:
        pass
        
    records_to_insert = []
    for item in mock_data:
        print(f"🧠 Generating embedding for: {item['commodity']}")
        embedding = get_embedding(item["commodity"])
        
        # Merge the original dictionary with the new embedding
        record = {**item, "cargo_embedding": embedding}
        records_to_insert.append(record)
        
    print(f"\n⬆️ Uploading {len(records_to_insert)} records to Supabase...")
    
    response = supabase.table("shipments").insert(records_to_insert).execute()
    
    if hasattr(response, 'data') and len(response.data) > 0:
        print("✅ Database successfully seeded!")
    else:
        print("⚠️ Insertion returned no data, but might have succeeded. Check Supabase Dashboard.")
