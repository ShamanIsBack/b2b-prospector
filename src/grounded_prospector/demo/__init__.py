"""Bundled offline data powering ``--demo``.

The people, companies and profile URLs in the bundled recordings are **invented**.
Shipping a recorded search for real individuals would put their personal data in
a public repository to no purpose — so the demo fixture mirrors the API's real
response *schema* while containing no real person.

The schema was taken from the installed ``google-genai`` models rather than from
the documentation. Nothing here is checked against a live response automatically;
``scripts/record_fixture.py`` records one on demand for anyone who needs to
confirm the two still agree, and writes it outside the repository.
"""

from pathlib import Path

DEMO_DIR = Path(__file__).parent
DEMO_BRIEF = DEMO_DIR / "search.yaml"

# Serper is the default backend, so --demo replays Serper-shaped responses.
DEMO_SERPER_RESPONSES = DEMO_DIR / "serper_responses.json"

# Gemini grounding interactions, kept so the secondary backend's parser is
# exercised offline too.
DEMO_INTERACTIONS = DEMO_DIR / "interactions.json"
