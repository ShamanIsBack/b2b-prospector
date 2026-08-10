"""Bundled offline data powering ``--demo``.

The people, agencies and profile URLs in ``interactions.json`` are **invented**.
Shipping a recorded search for real individuals would put their personal data in
a public repository to no purpose — so the demo fixture mirrors the API's real
response *schema* while containing no real person.

The schema was taken from the installed ``google-genai`` models, and a real
recorded response (``tests/fixtures/gemini_interaction_raw.json``, gitignored)
is used locally to verify the two agree.
"""

from pathlib import Path

DEMO_DIR = Path(__file__).parent
DEMO_BRIEF = DEMO_DIR / "search.yaml"

# Serper is the default backend, so --demo replays Serper-shaped responses.
DEMO_SERPER_RESPONSES = DEMO_DIR / "serper_responses.json"

# Gemini grounding interactions, kept so the secondary backend's parser is
# exercised offline too.
DEMO_INTERACTIONS = DEMO_DIR / "interactions.json"
