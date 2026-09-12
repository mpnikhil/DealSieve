"""Strands agents. Owned by W3/W12.

acquisition.py  build_acquisition_agent(session) -> strands.Agent   (tools bound to a ProcessingSession)
tools.py        ProcessingSession + the @tool functions: record_claims, analyze_document, underwrite,
                request_skeptic_review, request_diligence, request_price_adjustment, notify_human
skeptic.py      run_skeptic(session) -> SkepticReport       (independent Strands agent, structured output)
inspector.py    run_inspector(session, attachment, open_requests) -> DocumentAnalysis  (multimodal: text + photos)
"""
