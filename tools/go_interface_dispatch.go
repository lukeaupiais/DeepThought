// Command go_interface_dispatch lists, for every method of an interface
// declared under ROOT, the methods declared under ROOT that a call through it
// can run: those of each concrete type (or pointer to it) declared under ROOT
// whose method set implements the interface. One tab-separated row per pair:
// the interface method's file and line, then the concrete method's file and
// line (files relative to ROOT, lines of the method names).
//
// It type-checks the module's packages from source with the standard
// library's go/types (dependencies are read from the module cache, as `go
// list` finds them); it builds nothing and runs no code from ROOT. Packages
// that fail to type-check are reported on stderr and skipped. Directories the
// go tool ignores (a leading `_` or `.`, testdata) are skipped, files are
// filtered by build constraints for the host platform, and test files are
// included in their package.
//
// tools/impact_truth.py reads the rows (--dispatch) to treat a call of an
// interface method as a possible call of each implementation.
//
// Usage: go run tools/go_interface_dispatch.go ROOT > dispatch.tsv
package main

import (
	"fmt"
	"go/ast"
	"go/build"
	"go/importer"
	"go/parser"
	"go/token"
	"go/types"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func ignored(name string) bool {
	return strings.HasPrefix(name, "_") || strings.HasPrefix(name, ".") || name == "testdata"
}

type importerFunc func(path string) (*types.Package, error)

func (f importerFunc) Import(path string) (*types.Package, error) { return f(path) }

func main() {
	root, err := filepath.Abs(os.Args[1])
	if err != nil {
		panic(err)
	}
	// Package directories, each with its files grouped by package name (an
	// external test package `p_test` is checked on its own).
	dirs := map[string]bool{}
	err = filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			if path != root && ignored(entry.Name()) {
				return filepath.SkipDir
			}
			if path != root {
				if _, err := os.Stat(filepath.Join(path, "go.mod")); err == nil {
					return filepath.SkipDir // another module
				}
			}
			return nil
		}
		if strings.HasSuffix(path, ".go") {
			dirs[filepath.Dir(path)] = true
		}
		return nil
	})
	if err != nil {
		panic(err)
	}
	module := ""
	if data, err := os.ReadFile(filepath.Join(root, "go.mod")); err == nil {
		for _, line := range strings.Split(string(data), "\n") {
			if fields := strings.Fields(line); len(fields) == 2 && fields[0] == "module" {
				module = fields[1]
			}
		}
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
			// Only the files this platform builds (build tags, _windows.go).
			if match, err := build.Default.MatchFile(dir, entry.Name()); err != nil || !match {
				continue
			}
			file, err := parser.ParseFile(fset, filepath.Join(dir, entry.Name()), nil, parser.SkipObjectResolution)
			if err != nil {
				fmt.Fprintf(os.Stderr, "%s: unparsed\n", entry.Name())
				continue
			}
			groups[file.Name.Name] = append(groups[file.Name.Name], file)
		}
		parsed[dir] = groups
		return groups
	}
	// Packages of this module are checked here, once, so that a type and the
	// interface it implements are the same objects across packages; other
	// imports come from the source importer. An importing package sees its
	// dependency without that dependency's test files.
	checked := map[string]*types.Package{}
	var check func(dir, name string, tests bool) *types.Package
	var imp importerFunc = func(path string) (*types.Package, error) {
		if module != "" && (path == module || strings.HasPrefix(path, module+"/")) {
			dir := filepath.Join(root, filepath.FromSlash(strings.TrimPrefix(strings.TrimPrefix(path, module), "/")))
			for name := range parse(dir) {
				if !strings.HasSuffix(name, "_test") {
					if pkg := check(dir, name, false); pkg != nil {
						return pkg, nil
					}
				}
			}
			return nil, fmt.Errorf("no package in %s", dir)
		}
		return source.Import(path)
	}
	check = func(dir, name string, tests bool) *types.Package {
		key := fmt.Sprintf("%s\x00%s\x00%v", dir, name, tests)
		if pkg, ok := checked[key]; ok {
			return pkg
		}
		checked[key] = nil // an import cycle yields nil
		var files []*ast.File
		for _, file := range parse(dir)[name] {
			if tests || !strings.HasSuffix(fset.Position(file.Pos()).Filename, "_test.go") {
				files = append(files, file)
			}
		}
		var problems []string
		conf := types.Config{
			Importer: imp,
			Error:    func(err error) { problems = append(problems, err.Error()) },
		}
		path := name
		if module != "" && !strings.HasSuffix(name, "_test") {
			rel, _ := filepath.Rel(root, dir)
			path = strings.TrimSuffix(module+"/"+filepath.ToSlash(rel), "/.")
		}
		pkg, _ := conf.Check(path, fset, files, nil)
		if len(problems) > 0 {
			rel, _ := filepath.Rel(root, dir)
			fmt.Fprintf(os.Stderr, "%s (%s): %d type errors, first: %s\n", rel, name, len(problems), problems[0])
		}
		checked[key] = pkg
		return pkg
	}
	var named []*types.TypeName
	var ifaces []*types.TypeName
	order := []string{}
	for dir := range dirs {
		order = append(order, dir)
	}
	sort.Strings(order)
	collect := func(pkg *types.Package) {
		if pkg == nil {
			return
		}
		scope := pkg.Scope()
		for _, n := range scope.Names() {
			tn, ok := scope.Lookup(n).(*types.TypeName)
			if !ok || tn.IsAlias() {
				continue
			}
			if types.IsInterface(tn.Type()) {
				ifaces = append(ifaces, tn)
			} else {
				named = append(named, tn)
			}
		}
	}
	for _, dir := range order {
		groups := parse(dir)
		names := []string{}
		for name := range groups {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			// The package as its importers see it, and its declarations in
			// test files, which only the package with its tests holds.
			if !strings.HasSuffix(name, "_test") {
				collect(check(dir, name, false))
			}
			withTests := check(dir, name, true)
			if withTests != nil {
				scope := withTests.Scope()
				for _, n := range scope.Names() {
					tn, ok := scope.Lookup(n).(*types.TypeName)
					if !ok || tn.IsAlias() {
						continue
					}
					inTest := strings.HasSuffix(fset.Position(tn.Pos()).Filename, "_test.go") || strings.HasSuffix(name, "_test")
					// The package's own interfaces are checked again with its
					// tests: a test-file type mentions this copy's types, not
					// the importer view's, so only this copy can match it.
					// Pairs found in both copies share positions and dedupe.
					if !inTest && !types.IsInterface(tn.Type()) {
						continue
					}
					if types.IsInterface(tn.Type()) {
						ifaces = append(ifaces, tn)
					} else {
						named = append(named, tn)
					}
				}
			}
		}
	}
	where := func(pos token.Pos) (string, int, bool) {
		p := fset.Position(pos)
		rel, err := filepath.Rel(root, p.Filename)
		if err != nil || strings.HasPrefix(rel, "..") {
			return "", 0, false
		}
		return filepath.ToSlash(rel), p.Line, true
	}
	seen := map[string]bool{}
	var rows []string
	for _, it := range ifaces {
		iface := it.Type().Underlying().(*types.Interface)
		if iface.NumMethods() == 0 {
			continue
		}
		for _, tn := range named {
			t := tn.Type()
			var impl types.Type
			if types.Implements(t, iface) {
				impl = t
			} else if ptr := types.NewPointer(t); types.Implements(ptr, iface) {
				impl = ptr
			} else {
				continue
			}
			for i := 0; i < iface.NumMethods(); i++ {
				m := iface.Method(i)
				fileI, lineI, ok := where(m.Pos())
				if !ok {
					continue
				}
				obj, _, _ := types.LookupFieldOrMethod(impl, true, m.Pkg(), m.Name())
				fn, ok := obj.(*types.Func)
				if !ok {
					continue
				}
				fileC, lineC, ok := where(fn.Pos())
				if !ok {
					continue
				}
				row := fmt.Sprintf("%s\t%d\t%s\t%d", fileI, lineI, fileC, lineC)
				if !seen[row] {
					seen[row] = true
					rows = append(rows, row)
				}
			}
		}
	}
	sort.Strings(rows)
	for _, row := range rows {
		fmt.Println(row)
	}
}
