"""Words that carry no fact, for the TRADING vertical.

The near-duplicate detector compares FACTS, not text. Two emails from one template share
every line of boilerplate — the same TO APPLY steps, the same disclaimer — so comparing
raw text measures the template and rates two completely different IPOs as near-identical.
Measured: word 4-grams scored genuine reposts at 0.167 while different-company pairs
reached 0.127. No threshold separates those. The fix was to score the extracted signature
instead, and this is the stop-list that makes a signature a signature.

Which words identify nothing is entirely niche-specific. "allotment", "gmp", "mainboard"
and "nse" mean nothing in a recipe vertical, and its own filler would mean nothing here.
So the SCORER lives in `core/delivery/policy.py` with its calibrated 0.50 threshold, and
the vocabulary lives here.

**Do not re-derive the threshold by editing this list.** 0.50 was calibrated against 127
real repost pairs and 3,000 negatives at the highest recall yielding zero false positives.
Adding or removing words moves every signature and therefore every score.
"""

# Kept as one string split on whitespace — the form it was written in, and the form that
# makes it reviewable as prose rather than as 140 quoted strings.
WORDS = set("""ipo new alert issue issues open opens opening close closes closing
date dates price band lot size market min minimum amount investment retail quota listing
listed face value fresh ofs offer sale registrar allotment refund credit shares share
per equity crore rupees the and for of to in on is are will be with full details detail
update mainboard sme nse bse limited ltd company apply subscribe gmp premium grey today
tomorrow day upcoming subscription qib hni nii employee tentative basis approx about now
soon tba what cost deadline source confirmed channel telegram money bank account you your
they their said advice not this that from more than blocked released days anything
first time public selling buy sell target stop loss price app angel groww section
approve payment request phone within minutes could lose nobody tell worth later
reports said reliable weaker middling folder""".split())
