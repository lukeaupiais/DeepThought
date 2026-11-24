// Command gofacts type-checks the Go packages of one module tree with the
// standard library's go/types and prints, as JSON lines, the facts that the
// data-contracts tool reads from Go:
//
//   - "file": every file the type checker read, with its package name.
//   - "call": every method call, with its receiver's type, the package
//     declaring the method (also when promoted from an embedded field), the
//     packages of its named result types, and for each of its first three
//     arguments its named type (through pointers and slices) and the string
//     value it has: a constant expression's, or that of a local variable
//     assigned exactly once, of a + join (operands without a value written
//     as Placeholder) or of sqlx's Rebind.
//   - "table_name": every TableName method with no parameters whose body is
//     one return of a constant string.
//   - "error": a package that did not type-check completely; its facts are
//     still printed where go/types could type them.
//
// It builds nothing and runs no code of the module: packages of the module
// are parsed and checked from source, and other imports come from the
// source importer, which reads the module cache (populate it with `go mod
// download`; a missing dependency only leaves its types unknown). Directories
// the go tool ignores (a leading `_` or `.`, testdata, vendor) and nested
// modules are skipped; files are filtered by build constraints for the host
// platform, and test files are checked with their package.
//
// Usage: gofacts ROOT
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/build"
	"go/constant"
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

type namedType struct {
	Package string `json:"package"`
	Name    string `json:"name"`
	File    string `json:"file,omitempty"`
	Line    int    `json:"line,omitempty"`
}

type argument struct {
	Text *string    `json:"text,omitempty"`
	Type *namedType `json:"type,omitempty"`
}

type fact struct {
	Kind      string     `json:"kind"`
	File      string     `json:"file,omitempty"`
	Directory string     `json:"directory,omitempty"`
	Line      int        `json:"line,omitempty"`
	Column    int        `json:"column,omitempty"`
	Method    string     `json:"method,omitempty"`
	Declared  string     `json:"declared,omitempty"`
	Results   []string   `json:"results,omitempty"`
	Receiver  *namedType `json:"receiver,omitempty"`
	Arguments []argument `json:"arguments,omitempty"`
	Type      *namedType `json:"type,omitempty"`
	Table     string     `json:"table,omitempty"`
	Package   string     `json:"package,omitempty"`
	Message   string     `json:"message,omitempty"`
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
			file, err := parser.ParseFile(fset, filepath.Join(dir, entry.Name()), nil, parser.SkipObjectResolution)
			if err != nil {
				continue
			}
			groups[file.Name.Name] = append(groups[file.Name.Name], file)
		}
		parsed[dir] = groups
		return groups
	}
	type checkedPackage struct {
		pkg   *types.Package
		info  *types.Info
		files []*ast.File
	}
	checked := map[string]*checkedPackage{}
	var check func(dir, name string, tests bool) *checkedPackage
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
				if c := check(dir, names[0], false); c != nil && c.pkg != nil {
					return c.pkg, nil
				}
			}
			return nil, fmt.Errorf("no package in %s", dir)
		}
		return source.Import(path)
	}
	check = func(dir, name string, tests bool) *checkedPackage {
		key := fmt.Sprintf("%s\x00%s\x00%v", dir, name, tests)
		if c, ok := checked[key]; ok {
			return c
		}
		checked[key] = nil // an import cycle yields nil
		var files []*ast.File
		for _, file := range parse(dir)[name] {
			if tests || !strings.HasSuffix(fset.Position(file.Pos()).Filename, "_test.go") {
				files = append(files, file)
			}
		}
		info := &types.Info{
			Types:      map[ast.Expr]types.TypeAndValue{},
			Defs:       map[*ast.Ident]types.Object{},
			Uses:       map[*ast.Ident]types.Object{},
			Selections: map[*ast.SelectorExpr]*types.Selection{},
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
		pkg, _ := conf.Check(path, fset, files, info)
		c := &checkedPackage{pkg, info, files}
		if tests && len(problems) > 0 {
			rel, _ := filepath.Rel(root, dir)
			emit(fact{Kind: "error", Directory: filepath.ToSlash(rel), Package: name, Message: fmt.Sprintf("%d type errors, first: %s", len(problems), problems[0])})
		}
		checked[key] = c
		return c
	}
	where := func(pos token.Pos) (string, int, int) {
		p := fset.Position(pos)
		rel, err := filepath.Rel(root, p.Filename)
		if err != nil || strings.HasPrefix(rel, "..") {
			return "", p.Line, p.Column
		}
		return filepath.ToSlash(rel), p.Line, p.Column
	}
	named := func(t types.Type) *namedType {
		for {
			switch u := t.(type) {
			case *types.Pointer:
				t = u.Elem()
				continue
			case *types.Slice:
				t = u.Elem()
				continue
			case *types.Array:
				t = u.Elem()
				continue
			}
			break
		}
		n, ok := types.Unalias(t).(*types.Named)
		if !ok || n.Obj().Pkg() == nil {
			return nil
		}
		file, line, _ := where(n.Obj().Pos())
		return &namedType{Package: n.Obj().Pkg().Path(), Name: n.Obj().Name(), File: file, Line: line}
	}
	for _, dir := range dirs {
		groups := parse(dir)
		names := []string{}
		for name := range groups {
			names = append(names, name)
		}
		sort.Strings(names)
		for _, name := range names {
			c := check(dir, name, true)
			if c == nil || c.pkg == nil {
				continue
			}
			for _, file := range c.files {
				f, _, _ := where(file.Pos())
				emit(fact{Kind: "file", File: f, Package: name})
				facts(file, c.info, emit, where, named)
			}
		}
	}
}

// facts prints the call and TableName facts of one file.
func facts(file *ast.File, info *types.Info, emit func(fact), where func(token.Pos) (string, int, int), named func(types.Type) *namedType) {
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		if fn.Recv != nil && fn.Name.Name == "TableName" && fn.Type.Params.NumFields() == 0 && len(fn.Body.List) == 1 {
			if ret, ok := fn.Body.List[0].(*ast.ReturnStmt); ok && len(ret.Results) == 1 {
				if value := info.Types[ret.Results[0]].Value; value != nil && value.Kind() == constant.String {
					if obj, ok := info.Defs[fn.Name].(*types.Func); ok {
						if t := named(obj.Type().(*types.Signature).Recv().Type()); t != nil {
							f, line, column := where(fn.Pos())
							emit(fact{Kind: "table_name", File: f, Line: line, Column: column, Type: t, Table: constant.StringVal(value)})
						}
					}
				}
			}
		}
	}
	// Calls anywhere in the file: functions, and func literals in package
	// variables (a migration table).
	assigned := assignments(file, info)
	ast.Inspect(file, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		selection := info.Selections[selector]
		if selection == nil || selection.Kind() != types.MethodVal {
			return true
		}
		f, line, column := where(call.Pos())
		out := fact{Kind: "call", File: f, Line: line, Column: column, Method: selector.Sel.Name, Receiver: named(selection.Recv())}
		if pkg := selection.Obj().Pkg(); pkg != nil {
			out.Declared = pkg.Path()
		}
		results := selection.Obj().Type().(*types.Signature).Results()
		for i := 0; i < results.Len(); i++ {
			if t := named(results.At(i).Type()); t != nil {
				out.Results = append(out.Results, t.Package)
			}
		}
		for i, arg := range call.Args {
			if i == 3 {
				break
			}
			a := argument{Type: named(info.TypeOf(arg))}
			if text, ok := stringValue(arg, info, assigned); ok {
				a.Text = &text
			}
			out.Arguments = append(out.Arguments, a)
		}
		emit(out)
		return true
	})
}

// assignments maps each local variable of a file's functions to the one expression
// assigned to it, or to nil when it is assigned more than once, assigned
// without one expression of its own, or has its address taken.
func assignments(body ast.Node, info *types.Info) map[*types.Var]ast.Expr {
	found := map[*types.Var]ast.Expr{}
	seen := map[*types.Var]int{}
	set := func(id *ast.Ident, value ast.Expr) {
		obj := info.Defs[id]
		if obj == nil {
			obj = info.Uses[id]
		}
		v, ok := obj.(*types.Var)
		if !ok || v.Parent() == nil || v.Pkg() == nil || v.Parent() == v.Pkg().Scope() {
			return // a field, or a package variable other files may assign
		}
		seen[v]++
		if seen[v] == 1 {
			found[v] = value
		} else {
			found[v] = nil
		}
	}
	ast.Inspect(body, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.AssignStmt:
			for i, left := range n.Lhs {
				id, ok := left.(*ast.Ident)
				if !ok {
					continue
				}
				var value ast.Expr
				if len(n.Lhs) == len(n.Rhs) && (n.Tok == token.ASSIGN || n.Tok == token.DEFINE) {
					value = n.Rhs[i]
				}
				set(id, value)
				if value == nil {
					set(id, nil) // compound or multi-value assignment
				}
			}
		case *ast.ValueSpec:
			for i, id := range n.Names {
				var value ast.Expr
				if len(n.Names) == len(n.Values) {
					value = n.Values[i]
				}
				set(id, value)
			}
		case *ast.RangeStmt:
			for _, e := range []ast.Expr{n.Key, n.Value} {
				if id, ok := e.(*ast.Ident); ok {
					set(id, nil)
					set(id, nil)
				}
			}
		case *ast.UnaryExpr:
			if id, ok := n.X.(*ast.Ident); ok && n.Op == token.AND {
				set(id, nil)
				set(id, nil)
			}
		case *ast.IncDecStmt:
			if id, ok := n.X.(*ast.Ident); ok {
				set(id, nil)
				set(id, nil)
			}
		}
		return true
	})
	return found
}

// Placeholder stands for text the code does not fix inside a + join.
const Placeholder = "\x00"

// stringValue is the string an argument has: a constant expression's
// value; that of the one expression assigned to a local variable assigned
// once; a + join of strings in which operands without a value are written
// as Placeholder (when at least one operand has one); or the value of the
// one argument of sqlx's Rebind, which only rewrites bind markers.
func stringValue(arg ast.Expr, info *types.Info, assigned map[*types.Var]ast.Expr) (string, bool) {
	return valueOf(arg, info, assigned, 0)
}

func valueOf(arg ast.Expr, info *types.Info, assigned map[*types.Var]ast.Expr, depth int) (string, bool) {
	if depth > 8 {
		return "", false
	}
	if value := info.Types[arg].Value; value != nil {
		if value.Kind() == constant.String {
			return constant.StringVal(value), true
		}
		return "", false
	}
	switch e := ast.Unparen(arg).(type) {
	case *ast.Ident:
		v, ok := info.Uses[e].(*types.Var)
		if !ok {
			return "", false
		}
		value, ok := assigned[v]
		if !ok || value == nil {
			return "", false
		}
		return valueOf(value, info, assigned, depth+1)
	case *ast.BinaryExpr:
		if e.Op != token.ADD {
			return "", false
		}
		t := info.TypeOf(e)
		if t == nil {
			return "", false
		}
		if b, ok := t.Underlying().(*types.Basic); !ok || b.Info()&types.IsString == 0 {
			return "", false
		}
		left, lok := valueOf(e.X, info, assigned, depth+1)
		right, rok := valueOf(e.Y, info, assigned, depth+1)
		if !lok && !rok {
			return "", false
		}
		if !lok {
			left = Placeholder
		}
		if !rok {
			right = Placeholder
		}
		return left + right, true
	case *ast.CallExpr:
		selector, ok := e.Fun.(*ast.SelectorExpr)
		if !ok || selector.Sel.Name != "Rebind" || len(e.Args) != 1 {
			return "", false
		}
		selection := info.Selections[selector]
		if selection == nil || selection.Obj().Pkg() == nil || !strings.HasPrefix(selection.Obj().Pkg().Path(), "github.com/jmoiron/sqlx") {
			return "", false
		}
		return valueOf(e.Args[0], info, assigned, depth+1)
	}
	return "", false
}
