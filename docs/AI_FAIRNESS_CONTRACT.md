# AI fairness and human-decision contract

## Pilot invariant

The pilot accepts **zero protected-trait contribution at the model-input
boundary**. A pair of résumés with identical professional evidence but
different names, age, sex/gender, nationality, ethnicity, religion, marital
status, photo, contact details, or precise address must become byte-identical
untrusted-evidence envelopes before a model request is created.

This is stricter and more reproducible than choosing a non-zero score-delta
tolerance. The paired-input regression suite is deterministic and applies to
every private or managed model route because all routes share the same gateway
boundary. Candidate evidence remains available to authorized humans in the
canonical record; only the model view is masked.

AI output is advisory. It cannot hire, reject, send an offer, or otherwise make
an irreversible hiring decision. Score-triggered workflow rules are prohibited
from moving an application to `hired` or `rejected`, including rules persisted
before the validation contract was tightened.

## Provider certification

Before a tenant enables a concrete private-vLLM endpoint or opts into managed
OpenRouter processing, the operator runs the same paired corpus through the
exact model, prompt version, locale, and decoding settings. Because protected
variants reach the provider as identical model input, any unequal response is
treated as provider nondeterminism and fails certification rather than being
accepted as a protected-trait tolerance.

The certification record includes tenant, provider, model, prompt version,
input hash, response hashes, token/cost provenance, date, reviewer, and repeat
count. Live provider certification is credential-dependent; the deterministic
gateway invariant is not.
