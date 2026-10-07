// Package blockcand is the block arm's candidate generation: from a store's block
// rows and a query, the rows nearest the query in Hamming distance, in the order
// recall ranks them.
//
// It returns integers and nothing else. The re-rank of these rows by their stored
// vectors stays in the Python server, because a cosine summed in another order is
// a different float; what is held here is which rows reach that re-rank and in
// what order, and that can be held exactly. The contract is
// docs/BLOCK_CANDIDATES_CONTRACT.md, and the conformance data the tests read,
// tests/golden/block_candidates.json, is observed from the Python implementation.
package blockcand

import (
	"math/bits"
	"sort"
)

// Row is one stored block: the record it belongs to, the record's isolation axes,
// the label of the model that produced its bits, and the bits.
type Row struct {
	Kind       string
	ParentID   int64
	BlockIndex int64
	AgentID    string
	ProjectID  string
	Channel    string
	Model      string
	Bits       []byte // nil when the row has no bit string yet
}

// Query is the query's bits and the slice of the store a call may read.
type Query struct {
	Bits      []byte
	AgentID   string    // exact; the empty string is the bucket of rows owned by no named agent
	ProjectID *string   // nil reads every project; "" the global pool alone; "X" X and the global pool
	Channel   string    // "" reads every channel; "X" X and the channel-global rows
	Models    [2]string // a row is read only under one of these two labels
}

// Caps are the server's bounds on one call. None is derived from how many rows
// the caller asked to receive.
type Caps struct {
	Examined  int // rows looked at, counted over the rows the filter admits
	PerParent int // of those, rows one record may take: its first, in block order
	Depth     int // nearest rows returned
}

// Hit is one row returned: which block, and its Hamming distance to the query.
type Hit struct {
	Kind       string
	ParentID   int64
	BlockIndex int64
	Distance   int
}

// Candidates returns the Depth rows nearest the query. rows must be in key
// order: kind (byte order), then parent id, then block index, which is the order
// the caps truncate in.
func Candidates(rows []Row, q Query, c Caps) []Hit {
	return nearest(measure(examine(rows, q, c, admits), q.Bits), c.Depth, before)
}

// admits says whether the filter lets a row be examined. A row without bits or
// under another model's label is not examined at all, so it spends neither cap.
func admits(r Row, q Query) bool {
	if r.Bits == nil {
		return false
	}
	if r.Model != q.Models[0] && r.Model != q.Models[1] {
		return false
	}
	if r.AgentID != q.AgentID {
		return false
	}
	if q.ProjectID != nil {
		if *q.ProjectID == "" {
			if r.ProjectID != "" {
				return false
			}
		} else if r.ProjectID != *q.ProjectID && r.ProjectID != "" {
			return false
		}
	}
	if q.Channel != "" && r.Channel != q.Channel && r.Channel != "" {
		return false
	}
	return true
}

// examine walks the rows in key order and keeps what the caps allow: at most
// PerParent of each record's admitted rows, and at most Examined rows in all,
// stopping when that many are kept.
func examine(rows []Row, q Query, c Caps, admit func(Row, Query) bool) []Row {
	var out []Row
	var kind string
	var parent int64
	taken, started := 0, false
	for _, r := range rows {
		if len(out) >= c.Examined {
			break
		}
		if !admit(r, q) {
			continue
		}
		if !started || r.Kind != kind || r.ParentID != parent {
			kind, parent, taken, started = r.Kind, r.ParentID, 0, true
		}
		taken++
		if taken > c.PerParent {
			continue
		}
		out = append(out, r)
	}
	return out
}

type measured struct {
	row      Row
	distance int
}

// measure takes the Hamming distance of every examined row of the query's width.
// A row of another width was examined, and spent its share of the caps, but has
// no distance: a different width is a different dimension.
func measure(rows []Row, query []byte) []measured {
	out := make([]measured, 0, len(rows))
	for _, r := range rows {
		if r.Bits == nil || len(r.Bits) != len(query) {
			continue
		}
		d := 0
		for i, b := range query {
			d += bits.OnesCount8(r.Bits[i] ^ b)
		}
		out = append(out, measured{r, d})
	}
	return out
}

// before is the written-down total order: distance, then kind, parent id and
// block index. The key is unique, so no two rows tie.
func before(a, b measured) bool {
	if a.distance != b.distance {
		return a.distance < b.distance
	}
	if a.row.Kind != b.row.Kind {
		return a.row.Kind < b.row.Kind
	}
	if a.row.ParentID != b.row.ParentID {
		return a.row.ParentID < b.row.ParentID
	}
	return a.row.BlockIndex < b.row.BlockIndex
}

func nearest(ms []measured, depth int, less func(a, b measured) bool) []Hit {
	sort.Slice(ms, func(i, j int) bool { return less(ms[i], ms[j]) })
	if len(ms) > depth {
		ms = ms[:depth]
	}
	hits := make([]Hit, len(ms))
	for i, m := range ms {
		hits[i] = Hit{m.row.Kind, m.row.ParentID, m.row.BlockIndex, m.distance}
	}
	return hits
}
