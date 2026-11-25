// Command gofacts type-checks the Go packages of one module tree with the
// standard library's go/types and prints, as JSON lines, the facts that the
// data-contracts tool reads from Go:
//
//   - "file": every file the type checker read, with its package name.
//   - "call": every method call, with its receiver's type, the package
//     declaring the method (also when promoted from an embedded field), the
//     packages of its named result types, and for each of its first three
//     arguments its named type (through pointers and slices) and the string
//     value it has: a constant expression's, that a local variable holds
//     there (followed through assignments, += and branches; see flow), of a
//   - join (operands without a value written as Placeholder) or of sqlx's
//     Rebind.
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
	Texts []string   `json:"texts,omitempty"`
	Type  *namedType `json:"type,omitempty"`
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
	flows := newFlow(file, info)
	for _, decl := range file.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			flows.function(d.Body)
		case *ast.GenDecl:
			flows.expressions(d, nil)
		}
	}
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
			if val := stringValue(arg, info, flows); val.known {
				a.Texts = val.texts
			}
			out.Arguments = append(out.Arguments, a)
		}
		emit(out)
		return true
	})
}

// Placeholder stands for text the code does not fix inside a string.
const Placeholder = "\x00"

// value is what a string variable holds at a point of the program: the
// texts it may hold, one per path that reaches the point (Placeholder where
// the code does not fix the text), or nothing known.
type value struct {
	texts []string
	known bool
}

var unknown = value{}

// paths is how many texts a value keeps before they are folded into one.
const paths = 16

func known(texts ...string) value {
	seen, out := map[string]bool{}, []string{}
	for _, t := range texts {
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	sort.Strings(out)
	if len(out) > paths {
		out = []string{fold(out)}
	}
	return value{out, true}
}

// fold is one text for many: their common start and end around a
// Placeholder.
func fold(texts []string) string {
	first := texts[0]
	i, j := len(first), len(first)
	for _, t := range texts[1:] {
		k := 0
		for k < i && k < len(t) && first[k] == t[k] {
			k++
		}
		i = k
		k = 0
		for k < j && k < len(t) && first[len(first)-1-k] == t[len(t)-1-k] {
			k++
		}
		j = k
	}
	if i+j > len(first) {
		j = len(first) - i
	}
	return first[:i] + Placeholder + first[len(first)-j:]
}

// join is the value a variable holds where two paths meet.
func join(a, b value) value {
	if !a.known || !b.known {
		return unknown
	}
	return known(append(append([]string{}, a.texts...), b.texts...)...)
}

// concat is the value of a + b, each text of one followed by each of the
// other.
func concat(a, b value) value {
	out := []string{}
	for _, x := range a.texts {
		for _, y := range b.texts {
			out = append(out, x+y)
		}
	}
	return known(out...)
}

type state map[*types.Var]value

func (s state) copy() state {
	out := state{}
	for k, v := range s {
		out[k] = v
	}
	return out
}

// merge joins the states of the paths that reach one point.
func merge(states []state) state {
	if len(states) == 0 {
		return nil // no path reaches it
	}
	out := states[0].copy()
	for _, other := range states[1:] {
		for k, v := range out {
			if w, ok := other[k]; ok {
				out[k] = join(v, w)
			} else {
				out[k] = unknown
			}
		}
		for k := range other {
			if _, ok := out[k]; !ok {
				out[k] = unknown
			}
		}
	}
	return out
}

// flow follows the string values of a function's local variables through
// its statements, in order (constant propagation): an assignment sets a
// value, += appends to it, the branches of an if or switch are joined, and
// a variable assigned in a loop body is not known in or after the loop. A
// variable whose address is taken, that a func literal assigns, or that a
// function with goto or labels holds, is never known. It records the value
// each use of a variable reads.
type flow struct {
	info    *types.Info
	escaped map[*types.Var]bool
	uses    map[*ast.Ident]value
}

func newFlow(file *ast.File, info *types.Info) *flow {
	f := &flow{info: info, escaped: map[*types.Var]bool{}, uses: map[*ast.Ident]value{}}
	// Variables a func literal assigns, or whose address is taken.
	var visit func(node ast.Node, inside []*ast.FuncLit)
	visit = func(node ast.Node, inside []*ast.FuncLit) {
		ast.Inspect(node, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.FuncLit:
				if n != node {
					visit(n.Body, append(inside, n))
					return false
				}
			case *ast.UnaryExpr:
				if id, ok := ast.Unparen(n.X).(*ast.Ident); ok && n.Op == token.AND {
					if v := f.local(id); v != nil {
						f.escaped[v] = true
					}
				}
			case *ast.AssignStmt:
				if len(inside) > 0 {
					literal := inside[len(inside)-1]
					for _, left := range n.Lhs {
						if id, ok := left.(*ast.Ident); ok {
							if v := f.local(id); v != nil && !(literal.Pos() <= v.Pos() && v.Pos() < literal.End()) {
								f.escaped[v] = true
							}
						}
					}
				}
			}
			return true
		})
	}
	visit(file, nil)
	return f
}

// local is the local variable an identifier names, or nil.
func (f *flow) local(id *ast.Ident) *types.Var {
	obj := f.info.Defs[id]
	if obj == nil {
		obj = f.info.Uses[id]
	}
	v, ok := obj.(*types.Var)
	if !ok || v.IsField() || v.Pkg() == nil || v.Parent() == nil || v.Parent() == v.Pkg().Scope() {
		return nil
	}
	return v
}

// function follows one function body.
func (f *flow) function(body *ast.BlockStmt) {
	if body == nil {
		return
	}
	jumps := false
	ast.Inspect(body, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.FuncLit:
			return false
		case *ast.LabeledStmt:
			jumps = true
		case *ast.BranchStmt:
			if n.Tok == token.GOTO {
				jumps = true
			}
		}
		return true
	})
	if jumps {
		f.expressions(body, nil) // every variable stays unknown
		return
	}
	f.block(body.List, state{})
}

// expressions records the uses in a node with no variable known, and
// follows the func literals in it.
func (f *flow) expressions(node ast.Node, st state) {
	if node == nil {
		return
	}
	ast.Inspect(node, func(n ast.Node) bool {
		switch n := n.(type) {
		case *ast.FuncLit:
			f.function(n.Body)
			return false
		case *ast.Ident:
			if v := f.local(n); v != nil && f.info.Uses[n] != nil {
				if val, ok := st[v]; ok && !f.escaped[v] {
					f.uses[n] = val
				}
			}
		}
		return true
	})
}

// eval is the string value of an expression in a state.
func (f *flow) eval(e ast.Expr, st state) value {
	return valueOf(e, f.info, func(id *ast.Ident) (value, bool) {
		v := f.local(id)
		if v == nil || f.escaped[v] {
			return unknown, false
		}
		val, ok := st[v]
		return val, ok && val.known
	}, 0)
}

func (f *flow) assigned(nodes ...ast.Node) map[*types.Var]bool {
	found := map[*types.Var]bool{}
	for _, node := range nodes {
		if node == nil {
			continue
		}
		ast.Inspect(node, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.FuncLit:
				return false
			case *ast.AssignStmt:
				for _, left := range n.Lhs {
					if id, ok := left.(*ast.Ident); ok {
						if v := f.local(id); v != nil {
							found[v] = true
						}
					}
				}
			case *ast.RangeStmt:
				for _, e := range []ast.Expr{n.Key, n.Value} {
					if id, ok := e.(*ast.Ident); ok {
						if v := f.local(id); v != nil {
							found[v] = true
						}
					}
				}
			case *ast.IncDecStmt:
				if id, ok := n.X.(*ast.Ident); ok {
					if v := f.local(id); v != nil {
						found[v] = true
					}
				}
			}
			return true
		})
	}
	return found
}

// block follows a statement list from a state; it returns the state at its
// end, or nil when no path gets there (return, break, continue, panic).
func (f *flow) block(list []ast.Stmt, st state) state {
	for _, stmt := range list {
		if st == nil {
			return nil
		}
		st = f.statement(stmt, st)
	}
	return st
}

func (f *flow) statement(stmt ast.Stmt, st state) state {
	switch s := stmt.(type) {
	case *ast.AssignStmt:
		for _, right := range s.Rhs {
			f.expressions(right, st)
		}
		values := make([]value, len(s.Lhs))
		for i, left := range s.Lhs {
			id, ok := left.(*ast.Ident)
			if !ok {
				f.expressions(left, st)
				continue
			}
			switch {
			case len(s.Lhs) == len(s.Rhs) && (s.Tok == token.ASSIGN || s.Tok == token.DEFINE):
				values[i] = f.eval(s.Rhs[i], st)
			case s.Tok == token.ADD_ASSIGN && len(s.Rhs) == 1:
				if v := f.local(id); v != nil && st[v].known {
					f.uses[id] = st[v]
					right := f.eval(s.Rhs[0], st)
					if !right.known {
						right = known(Placeholder)
					}
					values[i] = concat(st[v], right)
				}
			}
		}
		for i, left := range s.Lhs {
			if id, ok := left.(*ast.Ident); ok {
				if v := f.local(id); v != nil {
					st[v] = values[i]
				}
			}
		}
		return st
	case *ast.DeclStmt:
		gen, ok := s.Decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.VAR {
			f.expressions(s, st)
			return st
		}
		for _, spec := range gen.Specs {
			spec := spec.(*ast.ValueSpec)
			for _, e := range spec.Values {
				f.expressions(e, st)
			}
			for i, id := range spec.Names {
				v := f.local(id)
				if v == nil {
					continue
				}
				switch {
				case len(spec.Values) == len(spec.Names):
					st[v] = f.eval(spec.Values[i], st)
				case len(spec.Values) == 0:
					if b, ok := v.Type().Underlying().(*types.Basic); ok && b.Info()&types.IsString != 0 {
						st[v] = known("") // a string's zero value
					} else {
						st[v] = unknown
					}
				default:
					st[v] = unknown
				}
			}
		}
		return st
	case *ast.ExprStmt:
		f.expressions(s, st)
		if call, ok := s.X.(*ast.CallExpr); ok {
			if id, ok := call.Fun.(*ast.Ident); ok && id.Name == "panic" && f.info.Uses[id] == types.Universe.Lookup("panic") {
				return nil
			}
		}
		return st
	case *ast.ReturnStmt:
		f.expressions(s, st)
		return nil
	case *ast.BranchStmt:
		return nil // the path continues after its loop or switch, already joined
	case *ast.BlockStmt:
		return f.block(s.List, st)
	case *ast.IfStmt:
		if s.Init != nil {
			st = f.statement(s.Init, st)
			if st == nil {
				return nil
			}
		}
		f.expressions(s.Cond, st)
		paths := []state{}
		if then := f.block(s.Body.List, st.copy()); then != nil {
			paths = append(paths, then)
		}
		if s.Else != nil {
			if other := f.statement(s.Else, st.copy()); other != nil {
				paths = append(paths, other)
			}
		} else {
			paths = append(paths, st)
		}
		return merge(paths)
	case *ast.SwitchStmt, *ast.TypeSwitchStmt, *ast.SelectStmt:
		var init ast.Stmt
		var body *ast.BlockStmt
		var tag ast.Node
		switch s := s.(type) {
		case *ast.SwitchStmt:
			init, body, tag = s.Init, s.Body, s.Tag
		case *ast.TypeSwitchStmt:
			init, body, tag = s.Init, s.Body, s.Assign
		case *ast.SelectStmt:
			body = s.Body
		}
		if init != nil {
			st = f.statement(init, st)
			if st == nil {
				return nil
			}
		}
		f.expressions(tag, st)
		paths, all := []state{}, false
		for _, clause := range body.List {
			var list []ast.Stmt
			switch c := clause.(type) {
			case *ast.CaseClause:
				for _, e := range c.List {
					f.expressions(e, st)
				}
				list, all = c.Body, all || c.List == nil
			case *ast.CommClause:
				f.expressions(c.Comm, st)
				list, all = c.Body, all || c.Comm == nil
			}
			end := f.block(list, st.copy())
			if end != nil {
				paths = append(paths, end)
			}
			// A break leaves the switch from somewhere in the clause.
			if len(f.breaks(list)) > 0 {
				paths = append(paths, f.forget(st.copy(), list))
			}
		}
		if !all || len(paths) == 0 {
			paths = append(paths, st)
		}
		return merge(paths)
	case *ast.ForStmt, *ast.RangeStmt:
		changed := f.assigned(s)
		for v := range changed {
			st[v] = unknown
		}
		switch s := s.(type) {
		case *ast.ForStmt:
			if s.Init != nil {
				f.expressions(s.Init, st)
			}
			f.expressions(s.Cond, st)
			f.expressions(s.Post, st)
			f.block(s.Body.List, st.copy())
		case *ast.RangeStmt:
			f.expressions(s.X, st)
			f.block(s.Body.List, st.copy())
		}
		return st
	case *ast.DeferStmt, *ast.GoStmt, *ast.SendStmt, *ast.IncDecStmt, *ast.EmptyStmt:
		f.expressions(s, st)
		for v := range f.assigned(s) {
			st[v] = unknown
		}
		return st
	}
	f.expressions(stmt, st)
	for v := range f.assigned(stmt) {
		st[v] = unknown
	}
	return st
}

// breaks lists the break statements of a clause body that leave its switch.
func (f *flow) breaks(list []ast.Stmt) []*ast.BranchStmt {
	found := []*ast.BranchStmt{}
	for _, stmt := range list {
		ast.Inspect(stmt, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.FuncLit, *ast.ForStmt, *ast.RangeStmt, *ast.SwitchStmt, *ast.TypeSwitchStmt, *ast.SelectStmt:
				return false
			case *ast.BranchStmt:
				if n.Tok == token.BREAK && n.Label == nil {
					found = append(found, n)
				}
			}
			return true
		})
	}
	return found
}

// forget is a state with every variable a statement list assigns unknown.
func (f *flow) forget(st state, list []ast.Stmt) state {
	for _, stmt := range list {
		for v := range f.assigned(stmt) {
			st[v] = unknown
		}
	}
	return st
}

// stringValue is the texts an argument may have: a constant expression's;
// those flow recorded for a variable; a + join of strings in which operands
// without a value are written as Placeholder (when at least one operand has
// one); or those of the one argument of sqlx's Rebind, which only rewrites
// bind markers.
func stringValue(arg ast.Expr, info *types.Info, f *flow) value {
	return valueOf(arg, info, func(id *ast.Ident) (value, bool) {
		val, ok := f.uses[id]
		return val, ok && val.known
	}, 0)
}

func valueOf(arg ast.Expr, info *types.Info, variable func(*ast.Ident) (value, bool), depth int) value {
	if depth > 8 {
		return unknown
	}
	if c := info.Types[arg].Value; c != nil {
		if c.Kind() == constant.String {
			return known(constant.StringVal(c))
		}
		return unknown
	}
	switch e := ast.Unparen(arg).(type) {
	case *ast.Ident:
		val, ok := variable(e)
		if !ok {
			return unknown
		}
		return val
	case *ast.BinaryExpr:
		if e.Op != token.ADD {
			return unknown
		}
		t := info.TypeOf(e)
		if t == nil {
			return unknown
		}
		if b, ok := t.Underlying().(*types.Basic); !ok || b.Info()&types.IsString == 0 {
			return unknown
		}
		left := valueOf(e.X, info, variable, depth+1)
		right := valueOf(e.Y, info, variable, depth+1)
		if !left.known && !right.known {
			return unknown
		}
		if !left.known {
			left = known(Placeholder)
		}
		if !right.known {
			right = known(Placeholder)
		}
		return concat(left, right)
	case *ast.CallExpr:
		selector, ok := e.Fun.(*ast.SelectorExpr)
		if !ok || selector.Sel.Name != "Rebind" || len(e.Args) != 1 {
			return unknown
		}
		selection := info.Selections[selector]
		if selection == nil || selection.Obj().Pkg() == nil || !strings.HasPrefix(selection.Obj().Pkg().Path(), "github.com/jmoiron/sqlx") {
			return unknown
		}
		return valueOf(e.Args[0], info, variable, depth+1)
	}
	return unknown
}
