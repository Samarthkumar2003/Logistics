import json, os
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

with open('domain_candidates.json') as f:
    domain_map = json.load(f)

domains = list(domain_map.keys())
results = []

BATCH = 20
for i in range(0, len(domains), BATCH):
    batch = domains[i:i+BATCH]
    lines = [f"{j+1}. {d} (e.g. {domain_map[d][0]})" for j, d in enumerate(batch)]
    domain_list = "\n".join(lines)
    prompt = (
        "For each domain below, identify the company. Return a JSON array (one object per domain), no markdown.\n"
        'Each object: {"domain": "...", "company_name": "...", "location": "city, country", '
        '"category": "CARRIER or CHA or FREIGHT_FORWARDER or CUSTOMER or OTHER", "keep": true or false}\n'
        "Set keep=false if it is NOT a logistics/shipping/freight company (e.g. manufacturer, retailer, bank).\n\n"
        "Domains:\n" + domain_list
    )
    resp = client.chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
        temperature=0,
        max_tokens=2000,
    )
    try:
        batch_results = json.loads(resp.choices[0].message.content.strip())
        results.extend(batch_results)
        kept = [r for r in batch_results if r.get('keep')]
        print(f"Batch {i//BATCH+1}: {len(kept)}/{len(batch)} logistics companies")
        for r in kept:
            print(f"  {r['category']:<20} {r['domain']:<35} {r['company_name']}")
    except Exception as e:
        print(f"Batch {i//BATCH+1} parse error: {e} | raw: {resp.choices[0].message.content[:200]}")

with open('domain_classified.json', 'w') as f:
    json.dump(results, f, indent=2)

kept_total = [r for r in results if r.get('keep')]
print(f"\nDone. {len(kept_total)} logistics domains identified out of {len(domains)} checked.")
