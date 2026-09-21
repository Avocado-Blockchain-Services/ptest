// Package probe supplies a miniature cooperative workload for candidate ptest.
package probe

import (
	"fmt"
	"os"
	"runtime"
	"syscall"
	"testing"
	"time"
)

func Run(t *testing.T, pkg string) {
	switch os.Getenv("PTEST_FIXTURE_MODE") {
	case "fail":
		t.Fatal("fixture assertion failure")
	case "exit":
		os.Exit(23)
	case "signal":
		_ = syscall.Kill(os.Getpid(), syscall.SIGTERM)
		t.Fatal("SIGTERM did not terminate the fixture")
	}
	for i := 0; i < 4; i++ {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			t.Parallel()
			mark(t, pkg, "start")
			time.Sleep(5 * time.Millisecond)
			mark(t, pkg, "end")
		})
	}
}

func mark(t *testing.T, pkg, phase string) {
	t.Helper()
	path := os.Getenv("PTEST_FIXTURE_TRACE")
	if path == "" {
		t.Fatal("Task11 must supply a run-owned trace path")
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	_, err = fmt.Fprintf(file, "%d %d %s %s %s %d\n", time.Now().UnixNano(),
		os.Getpid(), pkg, t.Name(), phase, runtime.GOMAXPROCS(0))
	if err != nil {
		t.Fatal(err)
	}
}
