package blockcand

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"reflect"
)

// GoldenFormat is the version of tests/golden/block_candidates.json this reader
// understands.
const GoldenFormat = 1

var (
	rowFields = []string{"kind", "parent_id", "block_index", "agent_id", "project_id", "channel", "model", "bits"}
	hitFields = []string{"kind", "parent_id", "block_index", "distance"}
)

// Case is one conformance case: rows in key order, a query, the caps, and the
// rows the Python implementation returned for them.
type Case struct {
	Name     string
	Note     string
	Caps     Caps
	Query    Query
	Rows     []Row
	Expected []Hit
}

// Generator is an implementation of the candidate generation under test.
type Generator func(rows []Row, q Query, c Caps) []Hit

// Mismatch is a case an implementation answered differently. At is the first
// position where the two lists differ, which is the length of the shorter one
// when one is a prefix of the other.
type Mismatch struct {
	Case string
	At   int
	Want []Hit
	Got  []Hit
}

func (m Mismatch) String() string {
	at := func(hits []Hit) string {
		if m.At < len(hits) {
			return fmt.Sprintf("%+v", hits[m.At])
		}
		return "nothing"
	}
	return fmt.Sprintf("%s: %d rows wanted, %d returned; at position %d wanted %s, got %s",
		m.Case, len(m.Want), len(m.Got), m.At, at(m.Want), at(m.Got))
}

// Verify runs gen on every case and returns the cases it answered differently.
func Verify(gen Generator, cases []Case) []Mismatch {
	var out []Mismatch
	for _, c := range cases {
		got := gen(c.Rows, c.Query, c.Caps)
		if reflect.DeepEqual(nonNil(got), nonNil(c.Expected)) {
			continue
		}
		at := 0
		for at < len(got) && at < len(c.Expected) && got[at] == c.Expected[at] {
			at++
		}
		out = append(out, Mismatch{c.Name, at, c.Expected, got})
	}
	return out
}

func nonNil(hits []Hit) []Hit {
	if hits == nil {
		return []Hit{}
	}
	return hits
}

type goldenFile struct {
	Format    int          `json:"format"`
	RowFields []string     `json:"row_fields"`
	HitFields []string     `json:"hit_fields"`
	Cases     []goldenCase `json:"cases"`
}

type goldenCase struct {
	Name string `json:"name"`
	Note string `json:"note"`
	Caps struct {
		Examined  int `json:"examined"`
		PerParent int `json:"per_parent"`
		Depth     int `json:"depth"`
	} `json:"caps"`
	Query struct {
		Bits      string    `json:"bits"`
		AgentID   string    `json:"agent_id"`
		ProjectID *string   `json:"project_id"`
		Channel   string    `json:"channel"`
		Models    [2]string `json:"models"`
	} `json:"query"`
	Rows     [][]json.RawMessage `json:"rows"`
	Expected [][]json.RawMessage `json:"expected"`
}

// LoadCases reads the conformance cases from the golden file at path.
func LoadCases(path string) ([]Case, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var g goldenFile
	if err := json.Unmarshal(raw, &g); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if g.Format != GoldenFormat {
		return nil, fmt.Errorf("%s: format %d, this reader understands %d", path, g.Format, GoldenFormat)
	}
	if !reflect.DeepEqual(g.RowFields, rowFields) || !reflect.DeepEqual(g.HitFields, hitFields) {
		return nil, fmt.Errorf("%s: fields %v / %v, this reader expects %v / %v",
			path, g.RowFields, g.HitFields, rowFields, hitFields)
	}
	cases := make([]Case, 0, len(g.Cases))
	for _, gc := range g.Cases {
		c := Case{
			Name: gc.Name,
			Note: gc.Note,
			Caps: Caps{Examined: gc.Caps.Examined, PerParent: gc.Caps.PerParent, Depth: gc.Caps.Depth},
			Query: Query{
				AgentID:   gc.Query.AgentID,
				ProjectID: gc.Query.ProjectID,
				Channel:   gc.Query.Channel,
				Models:    gc.Query.Models,
			},
		}
		if c.Query.Bits, err = hex.DecodeString(gc.Query.Bits); err != nil {
			return nil, fmt.Errorf("%s: query bits: %w", gc.Name, err)
		}
		for i, fields := range gc.Rows {
			r, err := decodeRow(fields)
			if err != nil {
				return nil, fmt.Errorf("%s: row %d: %w", gc.Name, i, err)
			}
			c.Rows = append(c.Rows, r)
		}
		for i, fields := range gc.Expected {
			h, err := decodeHit(fields)
			if err != nil {
				return nil, fmt.Errorf("%s: expected %d: %w", gc.Name, i, err)
			}
			c.Expected = append(c.Expected, h)
		}
		cases = append(cases, c)
	}
	return cases, nil
}

func decodeRow(fields []json.RawMessage) (Row, error) {
	var r Row
	if len(fields) != len(rowFields) {
		return r, fmt.Errorf("%d fields, want %d", len(fields), len(rowFields))
	}
	var bitsHex *string
	targets := []any{&r.Kind, &r.ParentID, &r.BlockIndex, &r.AgentID, &r.ProjectID, &r.Channel, &r.Model, &bitsHex}
	for i, t := range targets {
		if err := json.Unmarshal(fields[i], t); err != nil {
			return r, fmt.Errorf("%s: %w", rowFields[i], err)
		}
	}
	if bitsHex != nil {
		b, err := hex.DecodeString(*bitsHex)
		if err != nil {
			return r, fmt.Errorf("bits: %w", err)
		}
		r.Bits = b
	}
	return r, nil
}

func decodeHit(fields []json.RawMessage) (Hit, error) {
	var h Hit
	if len(fields) != len(hitFields) {
		return h, fmt.Errorf("%d fields, want %d", len(fields), len(hitFields))
	}
	targets := []any{&h.Kind, &h.ParentID, &h.BlockIndex, &h.Distance}
	for i, t := range targets {
		if err := json.Unmarshal(fields[i], t); err != nil {
			return h, fmt.Errorf("%s: %w", hitFields[i], err)
		}
	}
	return h, nil
}
