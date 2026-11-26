// Command gocheck type-checks every Go package of one module tree from
// source with the standard library's go/types and prints each type error as
// a JSON line: {"kind":"error","file":...,"line":...,"column":...,"message":...}.
// Each package also gets a {"kind":"package","directory":...,"package":...}
// line, and a package that could not be checked at all a
// {"kind":"unchecked",...} line with the reason.
//
// Unlike `go build` or `go vet`, a package with type errors does not stop
// its importers or its tests from being checked: go/types returns the
// package as far as it could type it, and importers are checked against
// that. So an edit that breaks a caller in its own package still reports
// the callers in every other package and test.
//
// It builds nothing and runs no code of the module: packages of the module
// are parsed and checked from source, test files with their package, and
// other imports come from the source importer, which reads the module cache.
// Directories the go tool ignores (a leading `_` or `.`, testdata, vendor)
// and nested modules are skipped; files are filtered by build constraints
// for the host platform.
//
// Usage: gocheck ROOT
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/build"
	"go/importer"
	"go/parser"
	"go/scanner"
	"go/token"
	"go/types"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type fact struct {
	Kind      string `json:"kind"`
	File      string `json:"file,omitempty"`
	Line      int    `json:"line,omitempty"`
	Column    int    `json:"column,omitempty"`
	Message   string `json:"message,omitempty"`
	Directory string `json:"directory,omitempty"`
	Package   string `json:"package,omitempty"`
}

func ignored(name string) bool {
	return strings.HasPrefix(name, "_") || strings.HasPrefix(name, ".") || name == "testdata" || name == "vendor"
}

type importerFunc func(path string) (*types.Package, error)

func (f importerFunc) Import(path string) (*types.Package, error) { return f(path) }

func main() {
	root, err := filepath.Abs(os.Args[1])
	if err != nil {
		panic(err)
	}
	out := bufio.NewWriter(os.Stdout)
	defer out.Flush()
	emit := func(f fact) {
		data, _ := json.Marshal(f)
		out.Write(data)
		out.WriteByte('\n')
	}
	dirs := []string{}
	filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if entry.IsDir() {
			if path != root && ignored(entry.Name()) {
				return filepath.SkipDir
			}
			if path != root {
				if _, err := os.Stat(filepath.Join(path, "go.mod")); err == nil {
					return filepath.SkipDir
				}
			}
			dirs = append(dirs, path)
		}
		return nil
	})
	sort.Strings(dirs)
	module := ""
	if data, err := os.ReadFile(filepath.Join(root, "go.mod")); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if fields := strings.Fields(line); len(fields) == 2 && fields[0] == "module" {
				module = strings.Trim(fields[1], `"`)
			}
		}
	}
	relative := func(path string) string {
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return filepath.ToSlash(path)
		}
		return filepath.ToSlash(rel)
	}
	fset := token.NewFileSet()
	source := importer.ForCompiler(fset, "source", nil)
	parsed := map[string]map[string][]*ast.File{}
	parse := func(dir string) map[string][]*ast.File {
		if groups, ok := parsed[dir]; ok {
			return groups
		}
		groups := map[string][]*ast.File{}
		entries, _ := os.ReadDir(dir)
		for _, entry := range entries {
			if entry.IsDir() || !strings.HasSuffix(entry.Name(), ".go") {
				continue
			}
			if match, err := build.Default.MatchFile(dir, entry.Name()); err != nil || !match {
				continue
			}
			// A file with syntax errors is still read as far as it parses;
			// its errors are reported like type errors.
			file, err := parser.ParseFile(fset, filepath.Join(dir, entry.Name()), nil, parser.SkipObjectResolution|parser.AllErrors)
			if list, ok := err.(scanner.ErrorList); ok {
				for _, e := range list {
					emit(fact{Kind: "error", File: relative(e.Pos.Filename), Line: e.Pos.Line, Column: e.Pos.Column, Message: e.Msg})
				}
			} else if err != nil {
				report(emit, relative, err)
			}
			if file == nil || file.Name == nil {
				continue
			}
			groups[file.Name.Name] = append(groups[file.Name.Name], file)
		}
		parsed[dir] = groups
		return groups
	}
	checked := map[string]*types.Package{}
	done := map[string]bool{}
	var check func(dir, name string, tests bool) *types.Package
	var imp importerFunc = func(path string) (*types.Package, error) {
		if module != "" && (path == module || strings.HasPrefix(path, module+"/")) {
			dir := filepath.Join(root, filepath.FromSlash(strings.TrimPrefix(strings.TrimPrefix(path, module), "/")))
			names := []string{}
			for name := range parse(dir) {
				if !strings.HasSuffix(name, "_test") && name != "main" {
					names = append(names, name)
				}
			}
			if len(names) == 1 {
				if pkg := check(dir, names[0], false); pkg != nil {
					return pkg, nil
				}
			}
			return nil, fmt.Errorf("no package in %s", dir)
		}
		return source.Import(path)
	}
	check = func(dir, name string, tests bool) *types.Package {
		key := fmt.Sprintf("%s\x00%s\x00%v", dir, name, tests)
		if done[key] {
			return checked[key]
		}
		done[key] = true // an import cycle yields nil
		var files []*ast.File
		for _, file := range parse(dir)[name] {
			if tests || !strings.HasSuffix(fset.Position(file.Pos()).Filename, "_test.go") {
				files = append(files, file)
			}
		}
		conf := types.Config{
			FakeImportC: true,
			Importer:    imp,
			// Errors are printed from the variant with tests, which holds
			// every file of the package; without this, an error in a
			// non-test file would print twice.
			Error: func(err error) {
				if tests {
					report(emit, relative, err)
				}
			},
		}
		path := name
		if module != "" && !strings.HasSuffix(name, "_test") {
			path = strings.TrimSuffix(module+"/"+relative(dir), "/.")
		}
		pkg, _ := conf.Check(path, fset, files, nil)
		checked[key] = pkg
		return pkg
	}
	for _, dir := range dirs {
		groups := parse(dir)
		names := []string{}
		for name := range groups {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			if pkg := check(dir, name, true); pkg == nil {
				emit(fact{Kind: "unchecked", Directory: relative(dir), Package: name, Message: "import cycle"})
				continue
			}
			emit(fact{Kind: "package", Directory: relative(dir), Package: name})
		}
	}
}

func report(emit func(fact), relative func(string) string, err error) {
	switch e := err.(type) {
	case types.Error:
		p := e.Fset.Position(e.Pos)
		emit(fact{Kind: "error", File: relative(p.Filename), Line: p.Line, Column: p.Column, Message: e.Msg})
	default:
		emit(fact{Kind: "error", Message: e.Error()})
	}
}
