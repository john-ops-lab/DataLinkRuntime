package main

import (
	"encoding/json"
	"math"
	"strconv"
	"strings"
)

// Handle is the runtime entry point for this adapter task. It inspects the
// "rows" array in the input, counts and sums the valid rows, and returns
// aggregate counts plus the running total.
//
// Acceptance rules for each row:
//   - id must be a string and non-empty after trimming surrounding whitespace.
//   - amount must be a finite, non-negative numeric value OR a non-empty
//     numeric string that parses to such a value; booleans are invalid.
//
// Invalid rows are skipped individually so a single bad row never aborts the
// task. If "rows" is missing or not an array, every output field is zero.
// Only the standard library is used; no credentials, network, file, or other
// external dependencies are touched.
func Handle(ctx *Context, input any) (any, error) {
	if input == nil {
		return zeroResult(), nil
	}

	payload, err := normalize(input)
	if err != nil {
		return zeroResult(), nil
	}

	rowsVal, present := payload["rows"]
	if !present || rowsVal == nil {
		return zeroResult(), nil
	}
	rows, ok := rowsVal.([]any)
	if !ok {
		return zeroResult(), nil
	}

	var accepted, invalid int
	var total float64

	for _, raw := range rows {
		row, ok := raw.(map[string]any)
		if !ok {
			invalid++
			continue
		}

		if _, ok := validateID(row["id"]); !ok {
			invalid++
			continue
		}

		amount, ok := validateAmount(row["amount"])
		if !ok {
			invalid++
			continue
		}

		accepted++
		total += amount
	}

	return map[string]any{
		"acceptedCount": accepted,
		"invalidCount":  invalid,
		"total":         total,
	}, nil
}

// zeroResult returns the canonical empty-state output for this task. Used
// whenever the input cannot be processed (nil input, malformed payload, or a
// missing/non-array "rows" field) so callers always receive the three
// documented keys with neutral values.
func zeroResult() map[string]any {
	return map[string]any{
		"acceptedCount": 0,
		"invalidCount":  0,
		"total":         0,
	}
}

// normalize round-trips arbitrary input through JSON so nested maps and
// slices surface as the generic any-based shapes this task expects (e.g.
// numbers as float64, arrays as []any). A normalization failure is treated
// as an empty input rather than a hard error so the task never aborts.
func normalize(v any) (map[string]any, error) {
	data, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// validateID returns the trimmed id and true only when v is a string with at
// least one non-whitespace character, matching the spec's "non-empty string
// after trimming" requirement.
func validateID(v any) (string, bool) {
	s, ok := v.(string)
	if !ok {
		return "", false
	}
	s = strings.TrimSpace(s)
	if s == "" {
		return "", false
	}
	return s, true
}

// validateAmount enforces the numeric rules: a finite non-negative numeric
// value, or a non-empty numeric string that parses to such a value.
// Booleans are explicitly invalid per the task specification, so they are
// excluded before the numeric branches. NaN and +/-Inf are rejected because
// they are not "finite".
func validateAmount(v any) (float64, bool) {
	switch n := v.(type) {
	case bool:
		// Per spec, boolean amounts are invalid.
		return 0, false
	case float64:
		if math.IsNaN(n) || math.IsInf(n, 0) || n < 0 {
			return 0, false
		}
		return n, true
	case json.Number:
		f, err := n.Float64()
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
			return 0, false
		}
		return f, true
	case string:
		s := strings.TrimSpace(n)
		if s == "" {
			return 0, false
		}
		f, err := strconv.ParseFloat(s, 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
			return 0, false
		}
		return f, true
	default:
		return 0, false
	}
}
