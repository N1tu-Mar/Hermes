"""Demo mode: fictional people + locally served pages, no network, no API key.

Every person here is invented and labeled "(demo)". Pages are served by the
app at /demo/pages/<slug> so source links in the UI actually open. One person
per list has an unreachable page and one has no public email, to exercise
failure isolation and the missing-contact state.
"""
import json
import re

import httpx

from .research import Fetcher

BASE = "http://127.0.0.1:8765/demo/pages"


def set_base(port):
    global BASE
    BASE = f"http://127.0.0.1:{port}/demo/pages"


# slug, name, org, role, interests, facts, email (None = not published), reachable
PEOPLE = {
    "research_professor": [
        ("avery-lin", "Dr. Avery Lin (demo)", "Rutgers University (demo)", "Associate Professor of Psychology",
         ["computational neurodevelopment", "infant EEG"],
         ["Runs the Developing Brain Modeling Lab, which builds computational models of infant attention.",
          "Lab page states the group welcomes undergraduate research assistants each semester."],
         "avery.lin@demo.example.edu", True),
        ("jordan-okafor", "Dr. Jordan Okafor (demo)", "Princeton University (demo)", "Assistant Professor of Neuroscience",
         ["neural network models of language acquisition", "developmental fMRI"],
         ["Studies how toddlers' language networks mature, using longitudinal fMRI.",
          "Teaches a seminar on computational models of development."],
         None, True),
        ("sam-reyes", "Dr. Sam Reyes (demo)", "Rutgers University (demo)", "Professor of Cognitive Science",
         ["Bayesian models of learning"], ["Develops Bayesian models of how children learn categories."],
         "sam.reyes@demo.example.edu", False),
    ],
    "startup": [
        ("mira-patel", "Mira Patel (demo)", "Lumen Health AI (demo)", "Co-founder & CTO",
         ["clinical NLP", "health data infrastructure"],
         ["Co-founded Lumen Health AI, which summarizes clinical notes for primary-care teams.",
          "Company careers page lists a summer engineering internship program."],
         "mira@lumen-demo.example.com", True),
        ("theo-grant", "Theo Grant (demo)", "Gridwise Energy (demo)", "Founder & CEO",
         ["battery scheduling", "climate tech"],
         ["Gridwise Energy builds scheduling software for community battery storage."],
         None, True),
    ],
    "speaker_mentor": [
        ("dana-kim", "Dana Kim (demo)", "Northbeam Ventures (demo)", "Partner",
         ["pre-seed investing", "student founders"],
         ["Partner at Northbeam Ventures focusing on pre-seed consumer software.",
          "Hosts a monthly office-hours series for first-time student founders."],
         "dana@northbeam-demo.example.com", True),
        ("luis-ortega", "Luis Ortega (demo)", "Formwork Robotics (demo)", "Founder",
         ["hardware startups", "manufacturing"],
         ["Founded Formwork Robotics after a student project, now building construction-site robots."],
         None, True),
        ("priya-shah", "Priya Shah (demo)", "Bay Product Collective (demo)", "Head of Product",
         ["product management careers"], ["Leads product at a Bay Area design collective."],
         "priya@bpc-demo.example.com", False),
    ],
}


def _all():
    for group in PEOPLE.values():
        yield from group


def page_html(slug):
    for s, name, org, role, interests, facts, email, reachable in _all():
        if s == slug and reachable:
            contact = f'<p>Email: <a href="mailto:{email}">{email}</a></p>' if email else "<p>Contact via department office.</p>"
            items = "".join(f"<li>{f}</li>" for f in facts)
            return (f"<html><head><title>{name}</title><style>body{{font-family:sans-serif}}</style></head><body>"
                    f"<nav>Demo site navigation</nav><h1>{name}</h1><p>{role}, {org}</p>"
                    f"<p><strong>Fictional demo page.</strong> Interests: {', '.join(interests)}.</p>"
                    f"<ul>{items}</ul>{contact}</body></html>")
    return None


def demo_fetcher(cache):
    def handler(request):
        m = re.search(r"/demo/pages/([\w-]+)$", request.url.path)
        html = page_html(m.group(1)) if m else None
        if html is None:
            return httpx.Response(503, text="unreachable (demo)")
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})
    return Fetcher(cache, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class DemoModel:
    """Same interface as OpenAIModel.structured, deterministic fixture output."""
    demo = True
    model = "demo-fixtures"

    def __init__(self, cache):
        self.cache = cache

    async def structured(self, campaign_id, instructions, user_input, schema_name, schema, web_search=False):
        from .openai_client import BudgetExceeded
        u = self.cache.usage(campaign_id)
        if u["api_calls"] >= u["budget"]:
            raise BudgetExceeded(f"campaign API budget of {u['budget']} calls used")
        self.cache.bump_usage(campaign_id, api_calls=1, input_tokens=len(user_input) // 4)
        if schema_name == "discovery":
            return self._discover(user_input)
        if schema_name == "profile":
            return self._profile(user_input)
        return self._draft(user_input), set()

    def _discover(self, text):
        brief = json.loads(text.split("Brief: ", 1)[1].split("\n", 1)[0])
        group = PEOPLE.get(brief.get("subtype") or "research_professor", PEOPLE["research_professor"])
        people = [{"name": n, "organization": o, "role": r, "profile_url": f"{BASE}/{s}",
                   "discovery_source_url": f"{BASE}/{s}", "fit_hint": f"Works on {i[0]}."}
                  for s, n, o, r, i, f, e, ok in group]
        return {"people": people}, {p["profile_url"] for p in people}

    def _profile(self, text):
        for s, name, org, role, interests, facts, email, ok in _all():
            if name in text:
                url = f"{BASE}/{s}"
                ev = [{"claim": f, "source_url": url} for f in facts]
                if not ok:  # unreachable page: model has nothing verifiable to cite
                    ev = [{"claim": facts[0], "source_url": "https://unverified.example.net/guess"}]
                return ({"contact_email": email, "contact_source_url": url if email else None,
                         "summary": f"{name} ({role}, {org}) works on {', '.join(interests)}.",
                         "research_interests": interests,
                         "fit_reason": f"Their work on {interests[0]} matches the requested focus.",
                         "evidence": ev}, {url} if ok else set())
        return {"contact_email": None, "contact_source_url": None, "summary": "", "research_interests": [],
                "fit_reason": "", "evidence": []}, set()

    def _draft(self, text):
        o = json.loads(text)["outline"]
        facts = o["evidence"]
        sender = o["sender_context"].split(".")[0]
        lines = [o["greeting"], "", f"{sender}."]
        lines.append(f"I came across this on your page: {facts[0]['claim']}")
        if o.get("specific_connection"):
            lines.append(o["specific_connection"])
        if o.get("event_details"):
            lines.append(f"Event details: {o['event_details']}.")
        lines += [f"I wanted to ask {o['ask']}.", "", o["signoff"], sender.split(",")[0].replace("I am ", "").replace("I'm ", "")]
        subject = o.get("template_subject") or {
            "research_professor": "Undergraduate research interest", "startup": "Quick note from a Rutgers student",
            "speaker_invite": "Speaker invitation from Rutgers", "rsvp_followup": "Following up on our invitation",
        }.get(o["template"], "A quick note from Rutgers")
        return {"subject": subject, "body": "\n".join(lines), "evidence_ids_used": [facts[0]["id"]]}
