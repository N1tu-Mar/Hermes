 ▐▛███▛█   Claude Code v2.1.280
▝▜██████▀  Sonnet 5 · Claude Pro
  ▝▝ ▝▝    ~/Downloads/email_workflow

  Introducing Opus 5.5
  Get to finished work sooner with Opus 5.5. Update Claude Code to try it.

❯ read through @"prompt (1).md" and start impolememting the core platform.      
  please make frequent commits every 3 minutes. THAT is a requirement. you HAVE 
  to make lots of commits.                                                      
  Start a three-minute timer immediately. While modifying files, create a       
  commit no later than three minutes after the previous commit. (start          
  building)                                                                     
  ⎿  Read prompt (1).md (234 lines)

⏺ Skill(ponytail)
  ⎿  Successfully loaded skill

  Ran 2 shell commands (ctrl+o to expand)
  ⎿  Interrupted · What should Claude do instead?
  ⎿  Interrupted · What should Claude do instead?

❯ /model opus                                                                   
  ⎿  Set model to Opus 5.5 and saved as your default for new sessions

❯ continue                                                                      

  Ran 1 shell command (ctrl+o to expand)

⏺ Starter JSON templates missing. Creating them per spec contract, then
  committing.

⏺ Write(candidates.json)
  ⎿  Wrote 6 lines to candidates.json
      1 {
      2   "schema_version": 1,
      3   "campaign_id": null,
      4   "intake": null,
      5   "candidates": []
      6 }

⏺ Write(research.json)
  ⎿  Wrote 5 lines to research.json
      1 {
      2   "schema_version": 1,
      3   "campaign_id": null,
      4   "profiles": {}
      5 }

⏺ Write(.gitignore)
  ⎿  Wrote 6 lines to .gitignore
      1 .env
      2 data/
      3 *.sqlite3
      4 __pycache__/
      5 .venv/
      6 .pytest_cache/

⏺ Write(.env.example)
  ⎿  Wrote 10 lines to .env.example
       1 # Leave OPENAI_API_KEY empty to run in demo mode (fixture data, 
         no network).
       2 OPENAI_API_KEY=
       4 # Local token required on mutating API routes. Generated at star
         tup if empty.
       5 APP_TOKEN=
       6 APP_PORT=8765
       8 # Gmail OAuth client secret + token live outside the repo.
       9 GMAIL_CREDENTIALS=~/.config/outreach/credentials.json
      10 GMAIL_TOKEN=~/.config/outreach/token.json
     … +2 lines (ctrl+o to expand)

⏺ Write(requirements.txt)
  ⎿  Wrote 8 lines to requirements.txt
      1 fastapi
      2 uvicorn
      3 httpx
      4 openai
      5 mcp
      6 google-api-python-client
      7 google-auth-oauthlib
      8 pytest

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/contracts.py)
  ⎿  Wrote 117 lines to app/contracts.py
      1 """Schema constants and validation for the two campaign JSON file
        s.
      2 
      3 Validation returns a list of problems instead of raising so one m
        alformed
      5 """
      6 import re
      7 from urllib.parse import urlsplit, urlunsplit
      8 
      9 SCHEMA_VERSION = 1
     … +129 lines (ctrl+o to expand)

⏺ Write(app/__init__.py)
  ⎿  Wrote 1 line to app/__init__.py
      1 (No content)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/storage.py)
  ⎿  Wrote 115 lines to app/storage.py
       1 """Campaign JSON repository: one candidates.json + research.json
       2 
       3 Only the app process writes these. Every update is a bounded
       4 read-modify-write under a per-campaign lock, written to a siblin
       5 fsynced and atomically replaced. That is O(n) file I/O and O(n) 
         memory per
         a
       7 schema_version bump) if campaigns grow far larger.
       8 """
      10 import os
     … +123 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Deps installed. 3 commits so far (contracts, storage). Now SQLite
  cache/drafts/jobs layer.

⏺ Write(app/cache.py)
  ⎿  Wrote 149 lines to app/cache.py
       1 """SQLite internal cache/index. Never a substitute for the campa
       2 
       3 Tables: page cache (by URL), research cache (by candidate + crit
         eria hash),
         obs for
       5 resumable progress, usage counters, and recent events for the pr
       6 """
       7 import json
       8 import sqlite3
      10 import time
     … +182 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/outlines.py)
  ⎿  Wrote 111 lines to app/outlines.py
       1 """Deterministic template routing and outline building. No LLM c
         alls here.
       2 
         plate's
       4 version when you change it: that invalidates earlier approvals.
       5 """
       6 
       8     "research_professor": {
       9         "version": "research_professor.v1",
      10         "audience": "professor",
     … +127 lines (ctrl+o to expand)

  Ran 2 shell commands (ctrl+o to expand)

⏺ Write(app/openai_client.py)  ⎿  Wrote 80 lines to app/openai_client.py
      1 """OpenAI Responses API adapter with per-campaign budget and usag
        e accounting.
      2 
      3 One client per process. Stable instructions go first so prompt ca
        ching can
      5 """
      6 import asyncio
      7 import json
      8 import os
      9 
     … +85 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Writing research worker (discovery, page fetch, evidence verification).

⏺ Write(app/research.py)
  ⎿  Wrote 246 lines to app/research.py
       1 """Discovery and research worker logic. Framework-free; provider
       2 
       3 Web text is untrusted data: it is only ever passed as quoted con
         tent, and
         odel actually
       5 cited or we actually fetched. Emails are only "verified" when th
       6 address appears on the fetched contact page.
       7 """
       8 import hashlib
      10 import re
     … +286 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/writer.py)
  ⎿  Wrote 90 lines to app/writer.py
       1 """Writing worker: outline + verified facts in, subject/body out
         , then rule checks.
       2 
         us flags the
       4 draft for review; nothing is ever sent.
       5 """
       6 import hashlib
       8 import re
       9 
      10 from .contracts import EMAIL_RE
     … +112 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/gmail.py)
  ⎿  Wrote 83 lines to app/gmail.py
      1 """Gmail adapter: create drafts only (scope gmail.compose). Never
         sends.
      2 
        meout can
      4 reconcile against existing drafts instead of creating a duplicate
        .
      5 """
      7 import os
      8 from email.message import EmailMessage
      9 from pathlib import Path
     … +90 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Now demo mode fixtures (fictional people, locally served pages so links work).
  ⎿  Interrupted · What should Claude do instead?

⏺ Unknown command: /usae. Did you mean /usage?

❯ continue                                                                      

⏺ Write(app/demo.py)
  ⎿  Wrote 151 lines to app/demo.py
      1 """Demo mode: fictional people + locally served pages, no network
        , no API key.
      3 Every person here is invented and labeled "(demo)". Pages are ser
        ved by the
      4 app at /demo/pages/<slug> so source links in the UI actually open
      5 per list has an unreachable page and one has no public email, to 
        exercise
      6 failure isolation and the missing-contact state.
      8 import json
      9 import re
     … +197 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/jobs.py)
  ⎿  Wrote 64 lines to app/jobs.py
      1 """Bounded worker coordinator: two research workers, one writer, 
        bounded queues.
      2 
        ueue gives
      4 backpressure without blocking the HTTP response). Job state lives
         in SQLite
      5 so a restart can mark interrupted work resumable.
      7 import asyncio
      8 import logging
      9 import secrets
     … +69 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Campaign service next, in two commits.

⏺ Write(app/campaigns.py)
  ⎿  Wrote 137 lines to app/campaigns.py
       1 """Campaign service: lifecycle, selection, approval policy, budg
         et, idempotency.
       3 The only place that decides state transitions. HTTP and MCP both
          call this
       4 through the API routes; domain modules below it know nothing abo
       5 """
       6 import asyncio
       7 import re
       9 from . import outlines, research, writer
      10 from .contracts import clean_intake, dedupe_key, normalize_url
     … +167 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Update(app/campaigns.py)
  ⎿  Added 347 lines
      135              except Exception as e:
      136                  out.append({"campaign_id": cid, "error": str(e
           )[:100]})
      137          return out
      138 +                                                              
      139 +    def _remaining(self, campaign_id):                        
      140 +        u = self.cache.usage(campaign_id)                     
      141 +        return u["budget"] - u["api_calls"]                   
      142 +                                                              
      143 +    def _active(self, campaign_id, kind):                     
      144 +        return [j for j in self.cache.jobs(campaign_id) if j["
          +kind"] == kind and j["status"] in ("queued", "running")]      
      145 +                                                              
      146 +    # --------------------------------------------------------
          +-------- discovery                                            
      147 +    def discover(self, campaign_id):                          
      148 +        self._require(campaign_id)                            
      149 +        if self._active(campaign_id, "discover"):             
      150 +            raise Rejected("discovery already running")       
      151 +        doc = self.store.candidates(campaign_id)              
      152 +        if len(doc["candidates"]) >= doc["intake"]["max_candid
          +ates"]:                                                       
      153 +            raise Rejected("candidate limit reached; raise max
          +_candidates to discover more")                                
      154 +        if self._remaining(campaign_id) < 1:                  
      155 +            raise Rejected("API budget exhausted for this camp
          +aign")                                                        
      156 +        return self.jobs.submit(campaign_id, "discover")      
      157 +                                                              
      158 +    async def _do_discover(self, campaign_id, _):             
      159 +        doc = self.store.candidates(campaign_id)              
      160 +        intake, existing = doc["intake"], doc["candidates"]   
      161 +        keys = {dedupe_key(c["name"], c.get("organization")) f
          +or c in existing} | \                                         
      162 +               {c["profile_url"] for c in existing if c.get("p
          +rofile_url")}                                                 
      163 +        people = await research.discover(self.model, campaign_
          +id, intake,                                                   
      164 +                                         intake["max_candidate
          +s"] - len(existing), keys)                                    
      165 +                                                              
      166 +        def add(doc):                                         
      167 +            nums = [int(c["candidate_id"][2:]) for c in doc["c
          +andidates"] if re.fullmatch(r"c_\d+", c["candidate_id"])]     
      168 +            n = max(nums, default=0)                          
      169 +            for p in people:                                  
      170 +                n += 1                                        
      171 +                doc["candidates"].append({"candidate_id": f"c_
          +{n:03d}", "name": p["name"],                                  
      172 +                                          "organization": p["o
          +rganization"], "role": p["role"],                             
      173 +                                          "profile_url": p["pr
          +ofile_url"],                                                  
      174 +                                          "discovery_source_ur
          +l": p["discovery_source_url"],                                
      175 +                                          "status": "discovere
          +d", "fit_hint": p["fit_hint"]})                               
      176 +        self.store.update_candidates(campaign_id, add)        
      177 +        self.cache.event(campaign_id, f"discovered {len(people
          +)} new candidates")                                           
      178 +                                                              
      179 +    # --------------------------------------------------------
          +-------- selection                                            
      180 +    def select(self, campaign_id, candidate_ids, action):     
      181 +        self._require(campaign_id)                            
      182 +        if action not in ("select", "exclude", "include"):    
      183 +            raise Rejected("action must be select, exclude, or
          + include")                                                    
      184 +        ids = set(candidate_ids)                              
      185 +                                                              
      186 +        def apply(doc):                                       
      187 +            changed = []                                      
      188 +            for c in doc["candidates"]:                       
      189 +                if c["candidate_id"] not in ids:              
      190 +                    continue                                  
      191 +                if action == "exclude":                       
      192 +                    c["status"] = "excluded"                  
      193 +                elif action == "include" and c["status"] == "e
          +xcluded":                                                     
      194 +                    c["status"] = "discovered"                
      195 +                elif action == "select" and c["status"] == "di
          +scovered":                                                    
      196 +                    c["status"] = "selected"                  
      197 +                else:                                         
      198 +                    continue                                  
      199 +                changed.append(c["candidate_id"])             
      200 +            return changed                                    
      201 +        return self.store.update_candidates(campaign_id, apply
          +)                                                             
      202 +                                                              
      203 +    # --------------------------------------------------------
          +-------- research                                             
      204 +    def _fresh_profile(self, profiles, cand_id):              
      205 +        p = profiles.get(cand_id)                             
      206 +        return p and p.get("status") in ("researched", "needs_
          +contact_review")                                              
      207 +                                                              
      208 +    def research(self, campaign_id, candidate_ids=None, refres
          +h=False):                                                     
      209 +        """Select + enqueue research. Skips excluded and alrea
          +dy-researched unless refresh."""                              
      210 +        self._require(campaign_id)                            
      211 +        cands = self.store.candidates(campaign_id)["candidates
          +"]                                                            
      212 +        profiles = self.store.research(campaign_id)["profiles"
          +]                                                             
      213 +        busy = {j["candidate_id"] for j in self._active(campai
          +gn_id, "research")}                                           
      214 +        wanted = set(candidate_ids) if candidate_ids else None
      215 +        todo = [c["candidate_id"] for c in cands              
      216 +                if c["status"] != "excluded" and c["candidate_
          +id"] not in busy                                              
      217 +                and (c["candidate_id"] in wanted if wanted els
          +e c["status"] in ("selected", "research_failed"))             
      218 +                and (refresh or not self._fresh_profile(profil
          +es, c["candidate_id"]))]                                      
      219 +        if len(todo) > self._remaining(campaign_id):          
      220 +            raise Rejected(f"{len(todo)} research calls projec
          +ted but only {self._remaining(campaign_id)} left in budget")  
      221 +        self.store.update_candidates(campaign_id, lambda d: [c
          +.update(status="selected") for c in d["candidates"]           
      222 +                                                             i
          +f c["candidate_id"] in todo])                                 
      223 +        for cid in todo:                                      
      224 +            if refresh:                                       
      225 +                self.cache.drop_research(f"{campaign_id}:{cid}
          +:")                                                           
      226 +        return {"queued": [self.jobs.submit(campaign_id, "rese
          +arch", cid) for cid in todo], "candidates": todo}             
      227 +                                                              
      228 +    def _set_status(self, campaign_id, cand_id, status, error=
          +None):                                                        
      229 +        def apply(doc):                                       
      230 +            for c in doc["candidates"]:                       
      231 +                if c["candidate_id"] == cand_id:              
      232 +                    c["status"] = status                      
      233 +                    if error:                                 
      234 +                        c["error"] = error                    
      235 +                    else:                                     
      236 +                        c.pop("error", None)                  
      237 +        self.store.update_candidates(campaign_id, apply)      
      238 +                                                              
      239 +    async def _do_research(self, campaign_id, cand_id):       
      240 +        doc = self.store.candidates(campaign_id)              
      241 +        cand = next((c for c in doc["candidates"] if c["candid
          +ate_id"] == cand_id), None)                                   
      242 +        if not cand or cand["status"] == "excluded":          
      243 +            return                                            
      244 +        compact = {k: cand.get(k) for k in ("candidate_id", "n
          +ame", "organization", "role",                                 
      245 +                                            "profile_url", "di
          +scovery_source_url")}                                         
      246 +        key = f"{campaign_id}:{cand_id}:{research.criteria_has
          +h(doc['intake'])}:{cand.get('profile_url')}"                  
      247 +        self._set_status(campaign_id, cand_id, "researching") 
      248 +        profile = self.cache.get_research(key)                
      249 +        if profile:                                           
      250 +            self.cache.bump_usage(campaign_id, cache_hits=1)  
      251 +        else:                                                 
      252 +            try:                                              
      253 +                profile = await research.research_candidate(se
          +lf.model, self.fetcher, campaign_id, doc["intake"], compact)  
      254 +            except (BudgetExceeded, ModelError) as e:         
      255 +                category = "budget" if isinstance(e, BudgetExc
          +eeded) else "model"                                           
      256 +                self._set_status(campaign_id, cand_id, "resear
          +ch_failed", f"{category}: {str(e)[:120]}")                    
      257 +                self.cache.event(campaign_id, f"{cand['name']}
          +: research failed ({category})")                              
      258 +                raise                                         
      259 +            if profile["status"] != "research_failed":        
      260 +                self.cache.put_research(key, profile)         
      261 +                                                              
      262 +        def save(rdoc):                                       
      263 +            rdoc["profiles"][cand_id] = profile               
      264 +        self.store.update_research(campaign_id, save)  # persi
          +sted immediately: a stop never loses it                       
      265 +        err = None if profile["status"] != "research_failed" e
          +lse "no sourced evidence (" + \                               
      266 +            ("; ".join(profile.get("notes", {}).get("fetch_err
          +ors", [])) or "nothing verifiable") + ")"                     
      267 +        self._set_status(campaign_id, cand_id, profile["status
          +"], err and err[:200])                                        
      268 +        self.cache.event(campaign_id, f"{cand['name']}: {profi
          +le['status']}")                                               
      269 +                                                              
      270 +    # --------------------------------------------------------
          +-------- drafting                                             
      271 +    def _outline(self, campaign_id, cand_id, followup):       
      272 +        intake = self.store.candidates(campaign_id)["intake"] 
      273 +        profile = self.store.research(campaign_id)["profiles"]
          +.get(cand_id)                                                 
      274 +        if not profile or profile.get("status") not in ("resea
          +rched", "needs_contact_review"):                              
      275 +            raise outlines.OutlineBlocked("no completed resear
          +ch profile")                                                  
      276 +        earlier = None                                        
      277 +        if followup:                                          
      278 +            inv = self.cache.get_draft(campaign_id, cand_id, o
          +utlines.TEMPLATES["speaker_invite"]["version"])               
      279 +            if inv and inv.get("invited_at"):                 
      280 +                earlier = {"invitation_subject": inv["subject"
          +], "invited_on": now_iso()[:10] if False else                 
      281 +                           __import__("datetime").datetime.fro
          +mtimestamp(inv["invited_at"]).strftime("%B %d")}              
      282 +        return outlines.build_outline(intake, profile, intake.
          +get("sender_background"), followup, earlier), profile         
      283 +                                                              
      284 +    def generate(self, campaign_id, candidate_ids, followup=Fa
          +lse):                                                         
      285 +        self._require(campaign_id)                            
      286 +        if not candidate_ids:                                 
      287 +            raise Rejected("pass explicit candidate_ids")     
      288 +        profiles = self.store.research(campaign_id)["profiles"
          +]                                                             
      289 +        ready = [c for c in candidate_ids if self._fresh_profi
          +le(profiles, c)]                                              
      290 +        return {"queued": [self.jobs.submit(campaign_id, "writ
          +e", f"{c}#followup" if followup else c) for c in ready],      
      291 +                "skipped_not_researched": sorted(set(candidate
          +_ids) - set(ready))}                                          
      292 +                                                              
      293 +    async def _do_write(self, campaign_id, arg):              
      294 +        cand_id, _, flag = arg.partition("#")                 
      295 +        followup = flag == "followup"                         
      296 +        intake = self.store.candidates(campaign_id)["intake"] 
      297 +        try:                                                  
      298 +            tname = outlines.route_template(intake, followup) 
      299 +            version = outlines.TEMPLATES[tname]["version"]    
      300 +            outline, profile = self._outline(campaign_id, cand
          +_id, followup)                                                
      301 +        except outlines.OutlineBlocked as e:                  
      302 +            version = locals().get("version") or "blocked"    
      303 +            self.cache.upsert_draft(campaign_id, cand_id, vers
          +ion, status="blocked", issues=[str(e)])                       
      304 +            self.cache.event(campaign_id, f"{cand_id}: draft b
          +locked ({e})")                                                
      305 +            return                                            
      306 +        h = writer.input_hash(outline)                        
      307 +        old = self.cache.get_draft(campaign_id, cand_id, versi
          +on)                                                           
      308 +        if old and old["status"] == "gmail_draft_created":    
      309 +            return  # never regenerate over an existing Gmail 
          +draft                                                         
      310 +        if old and old.get("input_hash") == h and old["status"
          +] in ("needs_review", "approved"):                            
      311 +            self.cache.bump_usage(campaign_id, cache_hits=1)  
      312 +            return  # same inputs: keep existing draft and its
          + approval                                                     
      313 +        d = await writer.write_draft(self.model, campaign_id, 
          +outline, profile)                                             
      314 +        self.cache.upsert_draft(campaign_id, cand_id, version,
          + input_hash=h, subject=d["subject"], body=d["body"],          
      315 +                                evidence_ids=d["evidence_ids"]
          +, outline=outline, issues=d["issues"],                        
      316 +                                status="needs_review")        
      317 +        self.cache.event(campaign_id, f"{profile['name']}: dra
          +ft ready for review"                                          
      318 +                         + (f" ({len(d['issues'])} flags)" if 
          +d["issues"] else ""))                                         
      319 +                                                              
      320 +    def edit_draft(self, campaign_id, cand_id, subject, body):
      321 +        d = self._draft(campaign_id, cand_id)                 
      322 +        if d["status"] in ("gmail_draft_created", "blocked"): 
      323 +            raise Rejected(f"cannot edit a draft in state {d['
          +status']}")                                                   
      324 +        issues = writer.check_draft(subject, body, d["outline"
          +], None, d["evidence_ids"])                                   
      325 +        self.cache.upsert_draft(campaign_id, cand_id, d["templ
          +ate_version"], subject=subject, body=body,                    
      326 +                                issues=issues, status="needs_r
          +eview")                                                       
      327 +        return self._draft(campaign_id, cand_id)              
      328 +                                                              
      329 +    def _draft(self, campaign_id, cand_id):                   
      330 +        self._require(campaign_id)                            
      331 +        d = self.cache.get_draft(campaign_id, cand_id)        
      332 +        if not d:                                             
      333 +            raise KeyError(f"no draft for {cand_id}")         
      334 +        return d                                              
      335 +                                                              
      336 +    def _still_current(self, campaign_id, d):                 
      337 +        """Approval is only valid while the outline inputs (te
          +mplate, evidence, ask, bio) are unchanged."""                 
      338 +        try:                                                  
      339 +            outline, _ = self._outline(campaign_id, d["candida
          +te_id"], d["template_version"].startswith("rsvp_followup"))   
      340 +        except outlines.OutlineBlocked:                       
      341 +            return False                                      
      342 +        return outline["template_version"] == d["template_vers
          +ion"] and writer.input_hash(outline) == d["input_hash"]       
      343 +                                                              
      344 +    def approve(self, campaign_id, cand_id):                  
      345 +        d = self._draft(campaign_id, cand_id)                 
      346 +        if d["status"] != "needs_review":                     
      347 +            raise Rejected(f"only drafts in needs_review can b
          +e approved (is {d['status']})")                               
      348 +        if not self._still_current(campaign_id, d):           
      349 +            self.cache.upsert_draft(campaign_id, cand_id, d["t
          +emplate_version"], issues=d["issues"] + ["inputs changed since
          + generation; regenerate"])                                    
      350 +            raise Rejected("evidence, template, ask, or backgr
          +ound changed since this draft was generated; regenerate first"
          +)                                                             
      351 +        self.cache.upsert_draft(campaign_id, cand_id, d["templ
          +ate_version"], status="approved")                             
      352 +        self.cache.event(campaign_id, f"{cand_id}: draft appro
          +ved")                                                         
      353 +        return self._draft(campaign_id, cand_id)              
      354 +                                                              
      355 +    def mark_invited(self, campaign_id, cand_id):             
      356 +        """User confirms they actually sent the invitation; en
          +ables an RSVP follow-up. Never inferred."""                   
      357 +        d = self.cache.get_draft(campaign_id, cand_id, outline
          +s.TEMPLATES["speaker_invite"]["version"])                     
      358 +        if not d or d["status"] not in ("approved", "gmail_dra
          +ft_created"):                                                 
      359 +            raise Rejected("only an approved speaker invitatio
          +n can be marked as sent")                                     
      360 +        self.cache.upsert_draft(campaign_id, cand_id, d["templ
          +ate_version"],                                                
      361 +                                invited_at=__import__("time").
          +time())                                                       
      362 +        return {"ok": True}                                   
      363 +                                                              
      364 +    # --------------------------------------------------------
          +-------- gmail                                                
      365 +    async def create_gmail_drafts(self, campaign_id, candidate
          +_ids):                                                        
      366 +        self._require(campaign_id)                            
      367 +        if not candidate_ids:                                 
      368 +            raise Rejected("pass explicit reviewed candidate_i
          +ds")                                                          
      369 +        profiles = self.store.research(campaign_id)["profiles"
          +]                                                             
      370 +        results = []                                          
      371 +        for cand_id in dict.fromkeys(candidate_ids):          
      372 +            d = self.cache.get_draft(campaign_id, cand_id)    
      373 +            r = {"candidate_id": cand_id}                     
      374 +            if not d or d["status"] not in ("approved", "gmail
          +_draft_created"):                                             
      375 +                r["result"] = "skipped: not approved"         
      376 +            elif d.get("gmail_draft_id"):                     
      377 +                r.update(result="already created", gmail_draft
          +_id=d["gmail_draft_id"])                                      
      378 +            elif not self._still_current(campaign_id, d):     
      379 +                self.cache.upsert_draft(campaign_id, cand_id, 
          +d["template_version"], status="needs_review")                 
      380 +                r["result"] = "skipped: inputs changed, re-rev
          +iew needed"                                                   
      381 +            elif not self.gmail:                              
      382 +                r.update(result="gmail not connected: preview 
          +only", preview=self.export_one(d, profiles.get(cand_id)))     
      383 +            else:                                             
      384 +                r.update(await self._gmail_one(campaign_id, d,
          + profiles.get(cand_id) or {}))                                
      385 +            results.append(r)                                 
      386 +        return results                                        
      387 +                                                              
      388 +    async def _gmail_one(self, campaign_id, d, profile):      
      389 +        key = f"{campaign_id}:{d['candidate_id']}:{d['template
          +_version']}"                                                  
      390 +        tv = d["template_version"]                            
      391 +        if d.get("gmail_attempt_at"):  # earlier attempt with 
          +unknown outcome: reconcile before retrying                    
      392 +            try:                                              
      393 +                found = await asyncio.to_thread(self.gmail.fin
          +d_by_key, key)                                                
      394 +            except Exception as e:                            
      395 +                return {"result": f"uncertain: reconcile faile
          +d ({type(e).__name__}); check Gmail Drafts manually"}         
      396 +            if found:                                         
      397 +                self.cache.upsert_draft(campaign_id, d["candid
          +ate_id"], tv, gmail_draft_id=found, status="gmail_draft_create
          +d")                                                           
      398 +                return {"result": "reconciled existing draft",
          + "gmail_draft_id": found}                                     
      399 +        to = profile.get("contact_email") if profile.get("emai
          +l_verified_on_page") else None                                
      400 +        self.cache.upsert_draft(campaign_id, d["candidate_id"]
          +, tv, gmail_attempt_at=__import__("time").time())             
      401 +        try:                                                  
      402 +            gid = await asyncio.to_thread(self.gmail.create, t
          +o, d["subject"], d["body"], key)                              
      403 +        except Exception as e:                                
      404 +            return {"result": f"failed ({type(e).__name__}); r
          +etry will reconcile first"}                                   
      405 +        self.cache.upsert_draft(campaign_id, d["candidate_id"]
          +, tv, gmail_draft_id=gid, status="gmail_draft_created")       
      406 +        self.cache.event(campaign_id, f"{d['candidate_id']}: G
          +mail draft created")                                          
      407 +        return {"result": "created" + ("" if to else " (no ver
          +ified recipient; add To in Gmail)"), "gmail_draft_id": gid}   
      408 +                                                              
      409 +    @staticmethod                                             
      410 +    def export_one(d, profile):                               
      411 +        to = (profile or {}).get("contact_email") if (profile 
          +or {}).get("email_verified_on_page") else ""                  
      412 +        return f"To: {to or '(missing - verify manually)'}\nSu
          +bject: {d['subject']}\n\n{d['body']}"                         
      413 +                                                              
      414 +    def export(self, campaign_id):                            
      415 +        self._require(campaign_id)                            
      416 +        profiles = self.store.research(campaign_id)["profiles"
          +]                                                             
      417 +        return "\n\n-----\n\n".join(self.export_one(d, profile
          +s.get(d["candidate_id"]))                                     
      418 +                                    for d in self.cache.list_d
          +rafts(campaign_id) if d["status"] in ("approved", "gmail_draft
          +_created"))                                                   
      419 +                                                              
      420 +    # --------------------------------------------------------
          +-------- views                                                
      421 +    def get(self, campaign_id):                               
      422 +        self._require(campaign_id)                            
      423 +        cdoc = self.store.candidates(campaign_id)             
      424 +        profiles = self.store.research(campaign_id)["profiles"
          +]                                                             
      425 +        drafts = {}                                           
      426 +        for d in self.cache.list_drafts(campaign_id):         
      427 +            if d["candidate_id"] not in drafts or d["updated_a
          +t"] > drafts[d["candidate_id"]]["updated_at"]:                
      428 +                drafts[d["candidate_id"]] = d                 
      429 +        rows = []                                             
      430 +        for c in cdoc["candidates"]:                          
      431 +            p, d = profiles.get(c["candidate_id"]) or {}, draf
          +ts.get(c["candidate_id"]) or {}                               
      432 +            rows.append({**c, "email": p.get("contact_email"),
          + "email_verified": p.get("email_verified_on_page", False),    
      433 +                         "fit_reason": p.get("fit_reason") or 
          +c.get("fit_hint"), "evidence_count": len(p.get("evidence") or 
          +[]),                                                          
      434 +                         "draft_status": d.get("status"), "dra
          +ft_flags": len(d.get("issues") or [])})                       
      435 +        return {"campaign_id": campaign_id, "intake": cdoc["in
          +take"], "candidates": rows,                                   
      436 +                "demo": getattr(self.model, "demo", False), "g
          +mail_connected": self.gmail is not None}                      
      437 +                                                              
      438 +    def detail(self, campaign_id, cand_id):                   
      439 +        self._require(campaign_id)                            
      440 +        cand = next((c for c in self.store.candidates(campaign
          +_id)["candidates"] if c["candidate_id"] == cand_id), None)    
      441 +        if not cand:                                          
      442 +            raise KeyError(cand_id)                           
      443 +        d = self.cache.get_draft(campaign_id, cand_id)        
      444 +        if d:                                                 
      445 +            d = {k: d[k] for k in ("template_version", "subjec
          +t", "body", "evidence_ids", "status", "issues",               
      446 +                                   "gmail_draft_id", "invited_
          +at") if k in d}                                               
      447 +        return {"candidate": cand, "profile": self.store.resea
          +rch(campaign_id)["profiles"].get(cand_id), "draft": d}        
      448 +                                                              
      449 +    def progress(self, campaign_id):                          
      450 +        self._require(campaign_id)                            
      451 +        counts, dcounts, jcounts = {}, {}, {}                 
      452 +        for c in self.store.candidates(campaign_id)["candidate
          +s"]:                                                          
      453 +            counts[c["status"]] = counts.get(c["status"], 0) +
          + 1                                                            
      454 +        for d in self.cache.list_drafts(campaign_id):         
      455 +            dcounts[d["status"]] = dcounts.get(d["status"], 0)
          + + 1                                                          
      456 +        for j in self.cache.jobs(campaign_id):                
      457 +            jcounts[j["status"]] = jcounts.get(j["status"], 0)
          + + 1                                                          
      458 +        u = self.cache.usage(campaign_id)                     
      459 +        return {"candidates": counts, "drafts": dcounts, "jobs
          +": jcounts,                                                   
      460 +                "usage": {k: u[k] for k in ("api_calls", "cach
          +e_hits", "input_tokens", "output_tokens", "budget")},         
      461 +                "stopped": campaign_id in self.jobs.stopped,  
      462 +                "recent": self.cache.events(campaign_id)}     
      463 +                                                              
      464 +    # --------------------------------------------------------
          +-------- control                                              
      465 +    def stop(self, campaign_id):                              
      466 +        self._require(campaign_id)                            
      467 +        self.jobs.stop(campaign_id)                           
      468 +        self.cache.event(campaign_id, "stopped by user")      
      469 +        return {"stopped": True}                              
      470 +                                                              
      471 +    def resume(self, campaign_id):                            
      472 +        """Re-queue interrupted/stopped work. Finished profile
          +s and drafts are skipped by the normal checks."""             
      473 +        self._require(campaign_id)                            
      474 +        stale = [j for j in self.cache.jobs(campaign_id) if j[
          +"status"] in ("interrupted", "stopped")]                      
      475 +        for j in stale:                                       
      476 +            self.cache.put_job(j["job_id"], campaign_id, j["ki
          +nd"], j["candidate_id"], "superseded")                        
      477 +        self.jobs.stopped.discard(campaign_id)                
      478 +        out = {"research": self.research(campaign_id)}        
      479 +        if any(j["kind"] == "discover" for j in stale):       
      480 +            out["discover"] = self.jobs.submit(campaign_id, "d
          +iscover")                                                     
      481 +        writes = {j["candidate_id"] for j in stale if j["kind"
          +] == "write"}                                                 
      482 +        out["write"] = [self.jobs.submit(campaign_id, "write",
          + w) for w in writes]                                          
      483 +        self.cache.event(campaign_id, "resumed")              
      484 +        return out                                            

  Ran 1 shell command (ctrl+o to expand)

⏺ Cleaning up inline imports.

  Ran 1 shell command (ctrl+o to expand)

⏺ API layer next.

⏺ Write(app/api.py)
  ⎿  Wrote 206 lines to app/api.py
       1 """HTTP routes: parse input, call CampaignService, return small 
       2 
       3 Bound to 127.0.0.1. All /api routes need the app token (X-App-To
         ken header),
          to a 0600
       5 file the MCP adapter reads. Host header is checked to block DNS 
       6 """
       7 import logging
       8 import os
      10 from contextlib import asynccontextmanager
     … +221 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Update(app/api.py)
  ⎿  Added 4 lines, removed 8 lines
       90          if request.url.path.startswith("/api/") and not secret
           s.compare_digest(
       91                  request.headers.get("x-app-token", ""), state.
           get("token", "")):
       92              return JSONResponse({"detail": "missing or bad app
            token"}, 401)
       93 -        try:                                                  
       94 -            return await call_next(request)                   
       95 -        except KeyError as e:                                 
       96 -            return JSONResponse({"detail": f"not found: {e}"},
          - 404)                                                         
       97 -        except Rejected as e:                                 
       98 -            return JSONResponse({"detail": str(e)}, 409)      
       99 -        except ValueError as e:                               
      100 -            return JSONResponse({"detail": str(e)}, 400)      
       93 +        return await call_next(request)                       
       94  
       95 +    for exc, code in ((KeyError, 404), (Rejected, 409), (Value
          +Error, 400)):                                                 
       96 +        app.add_exception_handler(exc, lambda r, e, code=code:
          + JSONResponse({"detail": str(e).strip("'")}, code))           
       97 +                                                              
       98      @app.get("/", response_class=HTMLResponse)
       99      def index():
      100          return FileResponse(APP_DIR / "templates" / "index.htm
           l")

⏺ Commit, then end-to-end test via TestClient.

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(tests/test_core.py)  ⎿  Wrote 267 lines to tests/test_core.py
       1 import asyncio
       2 import json
       3 import time
       4 
       5 import pytest
       6 from fastapi.testclient import TestClient
       7 
       8 from app import demo
       9 from app.api import create_app
      10 from app.cache import Cache
     … +351 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Sync handlers run in threadpool; submit via captured loop.

  Ran 1 shell command (ctrl+o to expand)

⏺ Fake shadows drafts() method with dict attribute. Fix test.

  Ran 1 shell command (ctrl+o to expand)

⏺ All 9 tests pass. Commit, then UI (loading frontend-design skill per spec).

  Ran 1 shell command (ctrl+o to expand)

⏺ Skill(frontend-design:frontend-design)
  ⎿  Successfully loaded skill

⏺ I'm going with a lab-notebook dossier style—cool paper tones with navy ink,
  scarlet reserved for flagged issues, and IBM Plex fonts, with claims backed by
  numbered footnote citations. Starting on the HTML now.

⏺ Write(app/templates/index.html)
  ⎿  Wrote 148 lines to app/templates/index.html
       1 <!doctype html>
       3 <head>
       4 <meta charset="utf-8">
       5 <meta name="viewport" content="width=device-width, initial-scale
       6 <title>Outreach Desk</title>
       7 <link rel="preconnect" href="https://fonts.googleapis.com">
         no:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600;700&
         family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesh
         eet">
      10 </head>
     … +181 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/static/app.css)
  ⎿  Wrote 141 lines to app/static/app.css
       1 :root {
       2   --paper: #e9edf1;       /* cool notebook paper */
       3   --sheet: #ffffff;
       4   --ink: #1b2433;         /* navy ink */
       5   --ink-soft: #56627a;
       7   --scarlet: #cc0033;     /* Rutgers scarlet: only for "needs yo
         u" */
       8   --verified: #2d7a57;
       9   --cite: #f3dc6b;        /* highlighter for citation markers */
      10   --focus: #2f6fd6;
     … +184 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(app/static/app.js)
  ⎿  Wrote 332 lines to app/static/app.js
      1 // Outreach Desk front end. Talks only to /api with the app token
      2 const $ = (s, el = document) => el.querySelector(s);
      3 const $$ = (s, el = document) => [...el.querySelectorAll(s)];
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39
        ;" }[c]));
        ;
      6 const LIST = ["organizations", "locations", "research_areas", "in
      7 const SUBTYPE_LABEL = { research_professor: "Research professors"
        , startup: "Startups", speaker_mentor: "Speakers & mentors" };
      9 // token: from ?t= once, then sessionStorage; strip from the addr
     … +470 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ MCP server next.

  Ran 2 shell commands (ctrl+o to expand)

⏺ Write(app/mcp_server.py)
  ⎿  Wrote 84 lines to app/mcp_server.py
       1 """Thin stdio MCP adapter. Calls the running app over loopback H
         TTP only.
       2 
         writer and
       4 enforces the same stage/approval rules as the UI.
       5 Run: python -m app.mcp_server   (with the app already running)
       6 """
       8 from pathlib import Path
       9 
      10 import httpx
     … +104 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Launch app, drive UI in browser.

  Read 1 file, called claude-in-chrome, ran 1 shell command (ctrl+o to expand)

⏺ Chrome extension not connected. Checking for Playwright; meanwhile README
  (commit due).

  Ran 1 shell command (ctrl+o to expand)

⏺ Write(README.md)
  ⎿  Wrote 157 lines to README.md
       1 # Outreach Desk
       2 
         people, and club speakers/mentors; research
       4 them from public sources; draft personalized emails; put approve
         d ones into Gmail **Drafts**. It never sends mail.
       6 One Python process (FastAPI + asyncio workers + SQLite), plain H
         TML/CSS/JS UI, optional stdio MCP adapter.
       7 
       9 
      10 ```bash
     … +198 lines (ctrl+o to expand)

  Ran 1 shell command (ctrl+o to expand)

⏺ Playwright in venv (dev only). Driving UI end-to-end with screenshots.

  Made 1 scratchpad edit +48, ran 1 shell command (ctrl+o to expand)

⏺ Flow works end to end, no console errors. Two bugs: greeting uses "(demo)" as
  last name; demo draft grammar. Checking screenshots.

  Read 2 files (ctrl+o to expand)

⏺ Fixes: strip "(demo)" in greeting names, demo wording, table column widths,
  eyebrow spacing.

  Ran 1 shell command (ctrl+o to expand)

⏺ Restart server with fixes; run acceptance script over HTTP (speaker flow,
  stop/restart, validation, RSS, MCP).

  Ran 1 shell command (ctrl+o to expand)
⏺ Background command ".venv/bin/python -m app.api" failed with exit code 144

  Made 1 scratchpad edit +63, ran 1 shell command (ctrl+o to expand)

⏺ The run looks solid—401 without token, safe restart, clean validation, 54 MB
  RSS, and root templates untouched—but I found that resume keeps retrying
  research_failed entries every time, wasting calls; those should only retry via
  an explicit per-person retry.

  Ran 1 shell command (ctrl+o to expand)

⏺ Smoke test MCP tools against running server.

  Ran 1 shell command (ctrl+o to expand)

⏺ MCP works; unapproved Gmail refused. Small UI tweak (retry label), then re-run
  UI script incl. mobile.

  Read 1 file, ran 1 shell command (ctrl+o to expand)

⏺ Mobile issues: preview <pre> overflows, status text clipped, failed profile
  shows unsourced summary/fit as facts, "Draft email" offered for failed,
  duplicate fetch attempt on same URL. Fixing.

  Ran 1 shell command (ctrl+o to expand)

⏺ Quick live check of real page fetcher (example.com), then final restart +
  test.

  Ran 2 shell commands (ctrl+o to expand)

⏺ The core platform works end to end in demo mode. I made 28 commits between
  19:56 and 20:10, and none were more than about 90 seconds apart. All 9 tests
  pass. The live OpenAI path and a real Gmail connection are still untested
  because there are no credentials yet.

  Before you start: candidates.json and research.json were not in the folder,
  even though the spec says they are the agreed starter files. I created them as
  empty templates (schema_version: 1). If you have the original versions, swap
  them in and check that the field names still match.

  Run it: .venv/bin/python -m app.api, then open the link it prints (it ends in
  ?t=<token>). The app is running now, and the current link is in
  scratchpad/server.log. Without OPENAI_API_KEY it runs in demo mode with
  made-up people labeled "(demo)". Their pages are served locally, so the source
  links open.

  What I checked:
  - Full UI flow in a headless browser: landing screen → Research → typed
    request → review and edit the parsed fields → discover 3 people → research →
    draft → edit → approve → Gmail step. With Gmail not connected, that step
    shows a preview instead. No console errors. I also checked it at desktop and
    phone widths and fixed overflow issues on the phone layout.
  - One failure stays with one person: the person whose page can't be reached
    ends as research_failed and the others finish. The person with no public
    email is shown as "missing". No address is guessed.
  - Stop and restart: after a hard server restart, interrupted jobs are marked
    resumable, and Resume re-runs only unfinished people. Finished profiles and
    unchanged drafts are not redone.
  - No duplicate Gmail drafts: tested with a mock Gmail. When a create call
    times out after Gmail already saved the draft, the retry finds it by a
    hidden header instead of making a second one.
  - Speaker invitations and follow-ups: a speaker campaign uses a different
    template from the professor one. An RSVP follow-up is blocked until you
    click "I sent this invitation".
  - Tool access for Claude Code (the MCP server): all 6 tools work over the
    local API. Gmail draft creation is refused for any draft you haven't
    approved.
  - Security and files: API calls without the token are rejected, and malformed
    campaign IDs are refused. Both campaign files pass validation after every
    run, the root templates stay unchanged, and the server used about 54 MB of
    memory.

  Still needed from you:
  - OpenAI: set OPENAI_API_KEY in .env. The code calling OpenAI's Responses API
    with web search and structured output has never run against the real API.
    Expect some fixes on the first live run.
  - Gmail: set up a Desktop OAuth client (steps are in the README), then run
    python -m app.gmail once. Only the mock has been tested.

  Limitations:
  - The request parser uses keyword rules rather than a model, so some fields
    may need correcting on the review screen before you confirm.
  - In demo mode, Stop is hard to catch mid-run because the fake work finishes
    almost instantly. The crash-and-resume test covers that case instead.
  - A draft is only counted as a previous invitation once you mark it as sent.
    The app never assumes a Gmail draft was sent.

  Tools: I used the ponytail and frontend-design skills. The design is a
  lab-notebook look with Rutgers scarlet used only for things that need your
  attention. The Chrome extension wasn't connected, so I installed Playwright in
  .venv (it's not in requirements.txt). CodeMunch is installed but I didn't use
  it, since the repo started empty and I wrote every file in this session.

✻ Sautéed for 10m 55s · done 8:11 PM

※ recap: You asked me to build the local research and outreach assistant, and it now works end to end in demo mode with all tests passing and frequent commits. Next, add your OpenAI key and set up Gmail so the untested live paths can run. (disable recaps in /config)

❯ /compact                                                                      
  ⎿  Compacted (ctrl+o to see full summary)
  ⎿  Read app/research.py (249 lines)
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -email-workflow/e7db0402-26e7-4b5f-97bc-7f541adfc37b/scratchpad/ui.py (48 
     lines)
  ⎿  Referenced file app/static/app.js
  ⎿  Read ../../../../private/tmp/claude-501/-Users-nityanthmaramreddy-Downloads
     -email-workflow/e7db0402-26e7-4b5f-97bc-7f541adfc37b/scratchpad/accept.py 
     (63 lines)
  ⎿  Read app/static/app.css (148 lines)
  ⎿  Skills restored (frontend-design:frontend-design, ponytail)