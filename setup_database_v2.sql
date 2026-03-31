-- =============================================================================
-- Migration v2: Add rfq_jobs and quotations tables
-- Target: Supabase (PostgreSQL)
-- =============================================================================

-- RFQ Jobs table: tracks each processed customer email
create table if not exists rfq_jobs (
    id bigint primary key generated always as identity,
    reference text unique not null,
    customer_email_sender text,
    customer_email_subject text,
    customer_email_body text,
    shipment_origin text,
    shipment_destination text,
    shipment_mode text,
    shipment_weight_kg numeric,
    shipment_commodity text,
    status text not null default 'rfqs_sent',
    agents_contacted text[],  -- array of agent names
    created_at timestamptz default now()
);

-- Quotations table: stores parsed rate quotation replies from agents
create table if not exists quotations (
    id bigint primary key generated always as identity,
    rfq_reference text not null references rfq_jobs(reference),
    agent_name text not null,
    agent_email text,
    rate numeric,
    currency text default 'USD',
    transit_time_days integer,
    validity text,
    terms text,
    raw_email_subject text,
    raw_email_body text,
    received_at timestamptz default now(),
    ai_assessment text,
    predicted_low numeric,
    predicted_high numeric,
    is_selected boolean default false
);

-- Indexes for fast lookups
create index if not exists idx_quotations_reference on quotations(rfq_reference);
create index if not exists idx_rfq_jobs_status on rfq_jobs(status);
create index if not exists idx_rfq_jobs_reference on rfq_jobs(reference);
create index if not exists idx_quotations_agent on quotations(agent_name);
