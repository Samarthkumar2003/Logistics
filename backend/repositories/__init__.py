"""
repositories
------------
The only package that knows Supabase exists.

Everything above this line works with the dataclasses in `backend.domain`.
That boundary is what makes services testable: swap a repository for a fake and
the business rules run with no network.

Repositories do queries and mapping. They must not draft emails, call an LLM, or
decide anything — that is what services are for.
"""
