package first

import (
	"example.com/ptest-fixture/probe"
	"testing"
)

func TestSuite(t *testing.T) { probe.Run(t, "first") }
