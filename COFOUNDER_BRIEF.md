# Logistics Copilot — Cofounder Brief

## The Problem

Freight forwarding is a high-volume, relationship-driven business. A mid-sized freight forwarder like Bhatia Shipping receives **50–100 emails per day** across three distinct types:

1. **Customer Requirements** — a shipper emails asking for a freight quote: origin, destination, cargo details, container type, weight.
2. **Agent Rate Cards** — freight carriers and forwarders reply with their rates, transit times, and validity.
3. **Operational Correspondence** — shipment updates, document approvals, booking confirmations, customs queries.

### The Manual Workflow (Before)

```
Customer emails → Staff reads → Manually emails 5–10 agents → 
Waits 1–2 days → Compares rates in inbox → Chooses best → 
Replies to customer → Sends acceptance/rejection to agents
```

**Pain points:**
- A single customer request triggers 5–10 outbound emails — written manually each time
- Rates arrive scattered across inbox threads — no single place to compare
- No price intelligence — staff don't know if a quoted rate is good or bad
- High repetition, low value work consuming skilled operations staff
- Responses delayed by hours or days — customers go elsewhere
- Knowledge locked in individual inboxes — no institutional memory of historical rates

---

## The Solution: Logistics Copilot

An AI-powered operations layer that sits on top of the existing Gmail inbox and automates the entire RFQ cycle — from customer email to rate comparison to agent approval — without changing how anyone communicates.

**No new email clients. No new workflows for customers or agents. The system plugs into the existing inbox.**

---

## How It Works — End to End

### Step 1: Inbox Monitoring (Every 5 Minutes)

The system polls the Gmail inbox automatically. Every new email is classified into one of three categories using a hybrid AI classifier:

| Label | Meaning |
|-------|---------|
| `customer_requirement` | A customer asking for a freight quote |
| `quotation_rate_card` | An agent providing rates in response to an RFQ |
| `general` | Operational mail — not actioned automatically |

**Classifier architecture:**
- **Tier 1 — Hard rules**: Known agent email addresses, internal job references (e.g. EN001116), our own RFQ reference numbers → instant, zero AI cost
- **Tier 2 — SVM on embeddings**: Email text is converted to a 1536-dimension vector (OpenAI `text-embedding-3-small`). An SVM model trained on 1000+ labeled examples predicts the label with confidence score.
- **Tier 3 — GPT few-shot**: If SVM confidence < 70%, GPT-4o-mini makes the final call with domain-specific examples.

---

### Step 2: Customer Requirement → RFQ Pipeline (Fully Automated)

When a `customer_requirement` email is detected:

```
1. Fetch full email body from Gmail
2. AI Intake Agent (GPT-4o) extracts:
      - Origin port / city
      - Destination port / country
      - Shipment mode (sea / air / road)
      - Commodity description
      - Weight (kg)
      - Container type
3. Agent Lookup — query database of 110 carriers & freight forwarders
   filtered by: destination country, shipping mode, commodity
4. RFQ Draft Generation — GPT-4o writes a professional RFQ email
   for each matched agent (personalised, references shipment specs)
5. Batch Email Send — RFQs dispatched to all matched agents via Gmail SMTP
6. Job Record created in database:
      - RFQ reference number (e.g. RFQ-20260607-a3f2)
      - Shipment details
      - List of agents contacted
      - Status: rfqs_sent
```

**Result**: A customer email received at 9am triggers personalised RFQs to 5–8 agents within 60 seconds, automatically.

---

### Step 3: Agent Rate Card → Quotation Parsing (Fully Automated)

When an agent replies with a rate card (`quotation_rate_card`):

```
1. Identify which open RFQ job this reply belongs to
   (by matching agent email to jobs where that agent was contacted)
2. Parse the rate card using GPT-4o:
      - Extract all rate tiers (20ft, 40ft, 40HC etc.)
      - Currency, transit time, validity, terms
3. Price Intelligence:
      - Look up historical rates for the same route/mode/commodity
      - Predict a fair price range using historical regression
      - Assess the quote: below_expected / within_range / above_expected
4. Store quotation in database with AI assessment
5. Update job status: quotes_received
```

---

### Step 4: Dashboard — Compare & Approve

The operations team opens the web dashboard and sees:

- **All emails classified** — inbox rendered with labels, confidence scores, full body on click
- **Shipments & RFQs** — every job card shows route, mode, commodity, agents contacted
- **Quotations panel** — all agent quotes side by side with:
  - Rate, currency, transit days, validity
  - AI price assessment (fair / above / below average)
  - Historical price range for that route
- **One-click Approve** — select the best quote → system sends:
  - Acceptance email to the winning agent
  - Polite rejection emails to all others (auto-generated, professional)

---

## Agent Database

110 real freight carriers and forwarders across India, UAE, China, Singapore, UK, USA and more.

| Category | Count | Examples |
|----------|-------|---------|
| Sea Freight Carriers | 55 | MSC, CMA CGM, Evergreen, COSCO, Maersk, HMM |
| Freight Forwarders | 46 | River Waves Connect, FBL India, Team Global, Emu Lines |
| Custom House Agents | 9 | Rightway Logistics, Pipil Freight, ATC, Aaron Logistics |

Agents are matched to each shipment by destination country, shipping mode, and commodity type.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend API | Python + FastAPI |
| Database | Supabase (PostgreSQL) |
| Email Integration | Gmail API (OAuth2 service account) |
| AI / LLM | OpenAI GPT-4o (extraction, drafts), GPT-4o-mini (classification fallback) |
| Embeddings | OpenAI text-embedding-3-small |
| Classifier | SVM RBF (scikit-learn) trained on 1000+ labeled examples |
| Scheduler | APScheduler — runs every 5 minutes |
| Frontend | Next.js 14 + TypeScript |
| Deployment | Local (production-ready for VPS/cloud) |

---

## What's Automated vs. What Needs Humans

| Task | Before | Now |
|------|--------|-----|
| Reading & classifying inbox | Manual, 2–3 hrs/day | Automatic |
| Writing & sending RFQs to agents | Manual, 20–30 min per enquiry | Automatic (<60 sec) |
| Tracking which agents replied | Mental overhead / spreadsheet | Automatic |
| Comparing rates | Manual inbox search | Dashboard, side by side |
| Price benchmarking | None | AI prediction from history |
| Sending acceptance / rejection emails | Manual | One click |
| Logging historical rates | None | Automatic |

**Humans still decide**: which quote to approve. Everything else runs automatically.

---

## Feedback Loop & Continuous Improvement

Every time an operator clicks "Correct label" on a misclassified email:
- The correction is stored with the email's embedding vector
- Added to the SVM training set
- Model improves on next inference

Historical approved quotations feed the price prediction model — the longer the system runs, the more accurate its rate benchmarks become for each route.

---

## Current Status

- ✅ Gmail inbox monitoring + classification (live)
- ✅ Automated RFQ generation and dispatch (live, test mode)
- ✅ Rate card parsing and quotation storage (live)
- ✅ Price prediction from historical data (live)
- ✅ Approval workflow with acceptance/rejection emails (live)
- ✅ Operations dashboard (live)
- ⏳ Outlook / Microsoft 365 support (architecture designed, ready to implement)
- ⏳ WhatsApp integration for customer requests (schema exists)
- ⏳ Multi-company / multi-mailbox support

---

## The Opportunity

Every freight forwarder in India runs this exact manual process. There are **~20,000 licensed customs house agents and freight forwarders in India alone**. None of them have tooling like this — they operate entirely on email threads, WhatsApp groups, and spreadsheets.

This is not a workflow tool. It is an **AI operations layer** that can be white-labelled and sold to any freight forwarder who receives inbound customer enquiries and solicits rates from a carrier network.

The moat is the agent database, the historical rate data, and the trained classifier — all of which compound in value the longer any customer runs the system.
