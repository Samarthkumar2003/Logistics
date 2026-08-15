"""
services
--------
Business rules. No FastAPI import anywhere in this package, and no direct
Supabase access — services call repositories and connectors.

That constraint is the whole point: a service can be exercised in a test with a
fake repository, which is impossible for a rule that lives inside a route
handler.
"""
