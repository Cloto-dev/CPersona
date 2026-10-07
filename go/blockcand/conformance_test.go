package blockcand

import (
	"path/filepath"
	"testing"
)

// The golden the Python implementation writes (scripts/capture-block-candidates.py).
var goldenPath = filepath.Join("..", "..", "tests", "golden", "block_candidates.json")

func loadCases(t *testing.T) []Case {
	t.Helper()
	cases, err := LoadCases(goldenPath)
	if err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("the golden holds no cases, so passing it proves nothing")
	}
	return cases
}

func TestCandidatesMatchTheGolden(t *testing.T) {
	cases := loadCases(t)
	for _, m := range Verify(Candidates, cases) {
		t.Error(m)
	}
}

// The verifier has teeth: an implementation that returns nothing fails every
// case that expects rows, and only those.
func TestTheVerifierReportsAnEmptyImplementation(t *testing.T) {
	cases := loadCases(t)
	expecting := 0
	for _, c := range cases {
		if len(c.Expected) > 0 {
			expecting++
		}
	}
	if expecting < len(cases)-2 {
		t.Fatalf("only %d of %d cases expect rows", expecting, len(cases))
	}
	empty := func([]Row, Query, Caps) []Hit { return nil }
	if got := len(Verify(empty, cases)); got != expecting {
		t.Fatalf("an empty implementation failed %d cases, want %d", got, expecting)
	}
}

func withCaps(change func(Caps) Caps) Generator {
	return func(rows []Row, q Query, c Caps) []Hit { return Candidates(rows, q, change(c)) }
}

func withAdmit(admit func(Row, Query) bool) Generator {
	return func(rows []Row, q Query, c Caps) []Hit {
		return nearest(measure(examine(rows, q, c, admit), q.Bits), c.Depth, before)
	}
}

func withOrder(less func(a, b measured) bool) Generator {
	return func(rows []Row, q Query, c Caps) []Hit {
		return nearest(measure(examine(rows, q, c, admits), q.Bits), c.Depth, less)
	}
}

// examineEveryRowShare counts a record's share over every row it holds, not over
// the rows the filter admits.
func examineEveryRowShare(rows []Row, q Query, c Caps) []Row {
	var out []Row
	var kind string
	var parent int64
	taken, started := 0, false
	for _, r := range rows {
		if len(out) >= c.Examined {
			break
		}
		if !started || r.Kind != kind || r.ParentID != parent {
			kind, parent, taken, started = r.Kind, r.ParentID, 0, true
		}
		taken++
		if !admits(r, q) || taken > c.PerParent {
			continue
		}
		out = append(out, r)
	}
	return out
}

// Every rule the contract states is held by some case: break one, and the golden
// says so.
func TestEveryMutantIsCaught(t *testing.T) {
	cases := loadCases(t)
	project := func(q Query) (string, bool) {
		if q.ProjectID == nil {
			return "", false
		}
		return *q.ProjectID, true
	}
	mutants := map[string]Generator{
		"share one larger":         withCaps(func(c Caps) Caps { c.PerParent++; return c }),
		"share one smaller":        withCaps(func(c Caps) Caps { c.PerParent--; return c }),
		"examined cap one larger":  withCaps(func(c Caps) Caps { c.Examined++; return c }),
		"examined cap one smaller": withCaps(func(c Caps) Caps { c.Examined--; return c }),
		"depth one larger":         withCaps(func(c Caps) Caps { c.Depth++; return c }),
		"ties by block index before parent id": withOrder(func(a, b measured) bool {
			if a.distance != b.distance {
				return a.distance < b.distance
			}
			if a.row.Kind != b.row.Kind {
				return a.row.Kind < b.row.Kind
			}
			if a.row.BlockIndex != b.row.BlockIndex {
				return a.row.BlockIndex < b.row.BlockIndex
			}
			return a.row.ParentID < b.row.ParentID
		}),
		"ties by kind descending": withOrder(func(a, b measured) bool {
			if a.distance != b.distance {
				return a.distance < b.distance
			}
			if a.row.Kind != b.row.Kind {
				return a.row.Kind > b.row.Kind
			}
			if a.row.ParentID != b.row.ParentID {
				return a.row.ParentID < b.row.ParentID
			}
			return a.row.BlockIndex < b.row.BlockIndex
		}),
		"rows without bits spend the caps": withAdmit(func(r Row, q Query) bool {
			if r.Bits == nil {
				r.Bits = []byte{}
			}
			return admits(r, q)
		}),
		"rows of another width spend nothing": withAdmit(func(r Row, q Query) bool {
			return len(r.Bits) == len(q.Bits) && admits(r, q)
		}),
		"model labels ignored": withAdmit(func(r Row, q Query) bool {
			r.Model = q.Models[0]
			return admits(r, q)
		}),
		"the legacy label refused": withAdmit(func(r Row, q Query) bool {
			return r.Model == q.Models[0] && admits(r, q)
		}),
		"agent ignored": withAdmit(func(r Row, q Query) bool {
			r.AgentID = q.AgentID
			return admits(r, q)
		}),
		"a project without the global pool": withAdmit(func(r Row, q Query) bool {
			if p, ok := project(q); ok && p != "" && r.ProjectID != p {
				return false
			}
			return admits(r, q)
		}),
		"the global pool reads every project": withAdmit(func(r Row, q Query) bool {
			if p, ok := project(q); ok && p == "" {
				q.ProjectID = nil
			}
			return admits(r, q)
		}),
		"channel ignored": withAdmit(func(r Row, q Query) bool {
			q.Channel = ""
			return admits(r, q)
		}),
		"a channel without the channel-global rows": withAdmit(func(r Row, q Query) bool {
			if q.Channel != "" && r.Channel != q.Channel {
				return false
			}
			return admits(r, q)
		}),
		"the share counted over every row": func(rows []Row, q Query, c Caps) []Hit {
			return nearest(measure(examineEveryRowShare(rows, q, c), q.Bits), c.Depth, before)
		},
	}
	for name, gen := range mutants {
		if len(Verify(gen, cases)) == 0 {
			t.Errorf("mutant survived: %s", name)
		}
	}
}
