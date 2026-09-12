"""Strands agents. Owned by W3.

acquisition.py  build_acquisition_agent(session) -> strands.Agent   (tools bound to a ProcessingSession)
tools.py        ProcessingSession + the @tool functions: record_claims, underwrite, request_skeptic_review,
                notify_human, draft_broker_questions
skeptic.py      run_skeptic(session) -> SkepticReport   (independent Strands agent, structured output)
"""
