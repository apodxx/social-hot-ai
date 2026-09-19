"""Pipeline services: the collect -> dedup -> store stage of the system.

Phase 2 implements the acquisition half (TikHub -> dedup -> PostgreSQL).
Phase 5's scheduler and Phase 3/4's AI stages plug into the same module without
changing the adapters.
"""
