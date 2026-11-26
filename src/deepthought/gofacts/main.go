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
//     A package variable initialized to a constant string has that value
//     when nothing in the package assigns it or takes its address and it is
//     unexported (or the package is main).
//     An argument that is a []string literal (or a local assigned one once)
//     has the value of each element instead; one that is field F of a local
//     assigned from one call (q, err := ch.QueueDeclare(...); q.Name) names
//     that call's position, result index and F.
//   - "literal": every keyed composite literal of a named struct type
//     declared in another package, with its type and, per key, the value its
//     element has, as for arguments (&x has x's value).
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
	"reflect"
	"sort"
	"strings"
)

type namedType struct {
	Package string `json:"package"`
	Name    string `json:"name"`
	File    string `json:"file,omitempty"`
	Line    int    `json:"line,omitempty"`
}

type resultField struct {
	Line   int    `json:"line"`
	Column int    `json:"column"`
	Index  int    `json:"index"`
	Field  string `json:"field"`
}

type argument struct {
	Texts   []string     `json:"texts,omitempty"`
	Strings [][]string   `json:"strings,omitempty"`
	Result  *resultField `json:"result,omitempty"`
	Type    *namedType   `json:"type,omitempty"`
	// The argument's static type is an interface: what it holds at run
	// time is not known.
	Interface bool `json:"interface,omitempty"`
	// "o:file:line:column" of the variable or field the argument names, or
	// "c:file:line:column" of the call it is (see reference).
	Ref string `json:"ref,omitempty"`
	// "f:file:line:column" of the module's function or method the argument
	// names, or that a call argument calls (a handler factory).
	Function string `json:"function,omitempty"`
	// The reference of the first parameter of a func literal argument.
	Literal string `json:"literal,omitempty"`
	// On a "pass" fact: the reference of the callee's parameter it fills.
	Parameter string `json:"parameter,omitempty"`
}

// guard is a type assertion a "pass" fact's call sits behind: the call
// runs only when the value Ref names holds Type ("if p, ok := v.(*T); ok"
// or a type switch case naming one type).
type guard struct {
	Ref       string     `json:"ref"`
	Type      *namedType `json:"type,omitempty"`
	Interface bool       `json:"interface,omitempty"`
}

// typedArgument is an argument a "pass" fact's call gives an interface
// parameter (v any): the named type of what it holds, when its static
// type is not an interface itself.
type typedArgument struct {
	Ref       string     `json:"ref,omitempty"`
	Parameter string     `json:"parameter"`
	Type      *namedType `json:"type,omitempty"`
	Interface bool       `json:"interface,omitempty"`
}

// A field of a struct type declared in the module: the module's named
// types its type holds (through pointers, slices, arrays, maps, channels,
// type arguments and inline structs), and whether it holds an interface,
// whose dynamic type the static type does not give.
type structField struct {
	Name      string      `json:"name"`
	Line      int         `json:"line"`
	Embedded  bool        `json:"embedded,omitempty"`
	Types     []namedType `json:"types,omitempty"`
	Interface bool        `json:"interface,omitempty"`
}

type element struct {
	Key string `json:"key"`
	argument
}

type fact struct {
	Kind      string        `json:"kind"`
	File      string        `json:"file,omitempty"`
	Directory string        `json:"directory,omitempty"`
	Line      int           `json:"line,omitempty"`
	Column    int           `json:"column,omitempty"`
	Method    string        `json:"method,omitempty"`
	Declared  string        `json:"declared,omitempty"`
	Results   []string      `json:"results,omitempty"`
	Receiver  *namedType    `json:"receiver,omitempty"`
	Arguments []argument    `json:"arguments,omitempty"`
	Type      *namedType    `json:"type,omitempty"`
	Elements  []element     `json:"elements,omitempty"`
	Fields    []structField `json:"fields,omitempty"`
	Table     string        `json:"table,omitempty"`
	Package   string        `json:"package,omitempty"`
	Message   string        `json:"message,omitempty"`
	// The reference of a method call's receiver expression, and of the
	// variable or field the call's result is assigned to.
	Operand  string `json:"operand,omitempty"`
	Assigned string `json:"assigned,omitempty"`
	// "f:file:line:column" of a "pass" fact's callee.
	Callee string `json:"callee,omitempty"`
	// The reference of the call itself ("c:..."), for a call or "new" fact.
	Self string `json:"self,omitempty"`
	// call: the types of the callee's results, the protobuf messages its
	// arguments and results carry, and where its name is written; signature: the named types of a function's parameters and
	// results.
	ResultTypes []*namedType `json:"result_types,omitempty"`
	Messages    []*namedType `json:"messages,omitempty"`
	NameLine    int          `json:"name_line,omitempty"`
	NameColumn  int          `json:"name_column,omitempty"`
	Types       []*namedType `json:"types,omitempty"`
	// "pass": the arguments given to interface parameters.
	Typed []typedArgument `json:"typed,omitempty"`
	// "pass": the type assertions the call runs behind.
	Guards []guard `json:"guards,omitempty"`
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
			// import "C" in the module's own cgo files: C.x is not resolved.
			FakeImportC: true,
			Importer:    imp,
			Error:       func(err error) { problems = append(problems, err.Error()) },
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
			fixed := packageValues(c.files, c.info, c.pkg)
			for _, file := range c.files {
				f, _, _ := where(file.Pos())
				emit(fact{Kind: "file", File: f, Package: name})
				facts(file, c.info, emit, where, named, c.pkg.Path(), fixed)
			}
		}
	}
}

// netHTTP reports whether t is net/http's *Request or ResponseWriter, what
// a net/http handler passes on to the functions that read the request body
// or write the response.
func netHTTP(t types.Type) bool {
	if p, ok := t.(*types.Pointer); ok {
		t = p.Elem()
	}
	n, ok := types.Unalias(t).(*types.Named)
	if !ok || n.Obj().Pkg() == nil || n.Obj().Pkg().Path() != "net/http" {
		return false
	}
	return n.Obj().Name() == "Request" || n.Obj().Name() == "ResponseWriter"
}

// namedIn is the named types a parameter or result type is built from:
// through pointers, slices, arrays, maps, channels, variadic parameters and
// the type arguments of generic types (grpc.ServerStreamingClient[Feature]),
// not into struct fields or function types.
func namedIn(t types.Type, named func(types.Type) *namedType) []*namedType {
	switch u := types.Unalias(t).(type) {
	case *types.Pointer:
		return namedIn(u.Elem(), named)
	case *types.Slice:
		return namedIn(u.Elem(), named)
	case *types.Array:
		return namedIn(u.Elem(), named)
	case *types.Chan:
		return namedIn(u.Elem(), named)
	case *types.Map:
		return append(namedIn(u.Key(), named), namedIn(u.Elem(), named)...)
	}
	n := named(t)
	if n == nil {
		return nil
	}
	found := []*namedType{n}
	if generic, ok := types.Unalias(t).(*types.Named); ok {
		for i := 0; i < generic.TypeArgs().Len(); i++ {
			found = append(found, namedIn(generic.TypeArgs().At(i), named)...)
		}
	}
	return found
}

// protoMessage reports whether t is a protobuf message type: its pointer
// has the ProtoReflect method that protoc-gen-go generates for every
// message (google.golang.org/protobuf/reflect/protoreflect.ProtoMessage).
func protoMessage(t types.Type) bool {
	n, ok := types.Unalias(t).(*types.Named)
	if !ok {
		return false
	}
	obj, _, _ := types.LookupFieldOrMethod(types.NewPointer(n), true, n.Obj().Pkg(), "ProtoReflect")
	method, ok := obj.(*types.Func)
	return ok && method.Type().(*types.Signature).Params().Len() == 0
}

// messagesIn is the protobuf message types a type is built from, as namedIn
// walks it.
func messagesIn(t types.Type, named func(types.Type) *namedType) []*namedType {
	found := []*namedType{}
	var walk func(types.Type)
	walk = func(t types.Type) {
		switch u := types.Unalias(t).(type) {
		case *types.Pointer:
			walk(u.Elem())
			return
		case *types.Slice:
			walk(u.Elem())
			return
		case *types.Array:
			walk(u.Elem())
			return
		case *types.Chan:
			walk(u.Elem())
			return
		case *types.Map:
			walk(u.Key())
			walk(u.Elem())
			return
		case *types.Named:
			if protoMessage(u) {
				if n := named(u); n != nil {
					found = append(found, n)
				}
			}
			for i := 0; i < u.TypeArgs().Len(); i++ {
				walk(u.TypeArgs().At(i))
			}
		}
	}
	walk(t)
	return found
}

// facts prints the call, signature and TableName facts of one file.
func facts(file *ast.File, info *types.Info, emit func(fact), where func(token.Pos) (string, int, int), named func(types.Type) *namedType, own string, fixed map[*types.Var]string) {
	structs(file, info, emit, where)
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok {
			continue
		}
		if obj, ok := info.Defs[fn.Name].(*types.Func); ok {
			sig := obj.Type().(*types.Signature)
			found := []*namedType{}
			for _, tuple := range []*types.Tuple{sig.Params(), sig.Results()} {
				for i := 0; i < tuple.Len(); i++ {
					found = append(found, namedIn(tuple.At(i).Type(), named)...)
				}
			}
			if len(found) > 0 {
				f, line, column := where(fn.Name.Pos())
				emit(fact{Kind: "signature", File: f, Line: line, Column: column, Types: found})
			}
		}
		if fn.Body == nil {
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
	flows.fixed = fixed
	for _, decl := range file.Decls {
		switch d := decl.(type) {
		case *ast.FuncDecl:
			flows.function(d.Body, state{})
		case *ast.GenDecl:
			flows.expressions(d, nil)
		}
	}
	literals, results := once(file, info)
	// References: a variable, struct field or package variable is named by
	// the position of its declaration, so every use of one object, in any
	// file of the module, gives the same text; a call by its own position.
	objectRef := func(obj types.Object) string {
		v, ok := obj.(*types.Var)
		if !ok {
			return ""
		}
		f, line, column := where(v.Pos())
		if f == "" {
			return ""
		}
		return fmt.Sprintf("o:%s:%d:%d", f, line, column)
	}
	var reference func(ast.Expr) string
	reference = func(e ast.Expr) string {
		switch x := ast.Unparen(e).(type) {
		case *ast.Ident:
			obj := info.Uses[x]
			if obj == nil {
				obj = info.Defs[x]
			}
			return objectRef(obj)
		case *ast.SelectorExpr:
			if selection := info.Selections[x]; selection != nil {
				if selection.Kind() == types.FieldVal {
					return objectRef(selection.Obj())
				}
				return ""
			}
			return objectRef(info.Uses[x.Sel])
		case *ast.CallExpr:
			// The opening parenthesis: a call and the call it is made on
			// (r.With(m).Get(...)) start at the same position.
			f, line, column := where(x.Lparen)
			return fmt.Sprintf("c:%s:%d:%d", f, line, column)
		case *ast.UnaryExpr:
			if x.Op == token.AND {
				return reference(x.X)
			}
		case *ast.StarExpr:
			return reference(x.X)
		}
		return ""
	}
	called := func(fun ast.Expr) *types.Func {
		var obj types.Object
		switch x := ast.Unparen(fun).(type) {
		case *ast.Ident:
			obj = info.Uses[x]
		case *ast.SelectorExpr:
			if selection := info.Selections[x]; selection != nil {
				obj = selection.Obj()
			} else {
				obj = info.Uses[x.Sel]
			}
		case *ast.IndexExpr: // an instantiated generic function
			return nil
		}
		fn, _ := obj.(*types.Func)
		return fn
	}
	functionRef := func(fn *types.Func) string {
		if fn == nil {
			return ""
		}
		f, line, column := where(fn.Pos())
		if f == "" {
			return ""
		}
		return fmt.Sprintf("f:%s:%d:%d", f, line, column)
	}
	// The function an argument names; for a call that passes no function
	// value, the function it calls, which returns the handler (a factory).
	// The module function a conversion to a func type wraps
	// (http.HandlerFunc(a.Token)), else nil.
	converted := func(e ast.Expr) *types.Func {
		call, ok := ast.Unparen(e).(*ast.CallExpr)
		if !ok || len(call.Args) != 1 || !info.Types[call.Fun].IsType() {
			return nil
		}
		if _, ok := info.Types[call.Fun].Type.Underlying().(*types.Signature); !ok {
			return nil
		}
		return called(call.Args[0])
	}
	// Variables assigned only such conversions of one function
	// (token := http.HandlerFunc(a.Token)), to the function.
	wrapping := map[types.Object]*types.Func{}
	reassigned := map[types.Object]bool{}
	bind := func(name ast.Expr, value ast.Expr) {
		id, ok := ast.Unparen(name).(*ast.Ident)
		if !ok {
			return
		}
		obj := info.Defs[id]
		if obj == nil {
			obj = info.Uses[id]
		}
		if obj == nil {
			return
		}
		fn := converted(value)
		if previous, seen := wrapping[obj]; fn == nil || (seen && previous != fn) {
			reassigned[obj] = true
		}
		wrapping[obj] = fn
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.AssignStmt:
			if len(n.Lhs) == len(n.Rhs) {
				for i := range n.Lhs {
					bind(n.Lhs[i], n.Rhs[i])
				}
			} else {
				for _, left := range n.Lhs {
					bind(left, nil)
				}
			}
		case *ast.ValueSpec:
			for i, name := range n.Names {
				if i < len(n.Values) {
					bind(name, n.Values[i])
				}
			}
		}
		return true
	})
	wrapped := func(id *ast.Ident) *types.Func {
		obj := info.Uses[id]
		if obj == nil || reassigned[obj] {
			return nil
		}
		return wrapping[obj]
	}
	function := func(arg ast.Expr) string {
		arg = ast.Unparen(arg)
		// A function converted to a handler type, directly, through a
		// variable holding the conversion, or through that variable's
		// ServeHTTP method value (token.ServeHTTP).
		if fn := converted(arg); fn != nil {
			return functionRef(fn)
		}
		if selector, ok := arg.(*ast.SelectorExpr); ok && selector.Sel.Name == "ServeHTTP" {
			if id, ok := ast.Unparen(selector.X).(*ast.Ident); ok {
				if fn := wrapped(id); fn != nil {
					return functionRef(fn)
				}
			}
		}
		if id, ok := arg.(*ast.Ident); ok {
			if fn := wrapped(id); fn != nil {
				return functionRef(fn)
			}
		}
		if call, ok := arg.(*ast.CallExpr); ok {
			// A call given a function (or a value of a func type such as
			// http.HandlerFunc) wraps it, as a middleware does: what runs
			// is not known.
			for _, inner := range call.Args {
				if t := info.TypeOf(inner); t != nil {
					if _, ok := t.Underlying().(*types.Signature); ok {
						return ""
					}
				}
			}
			arg = call.Fun
		}
		return functionRef(called(arg))
	}
	literalParameter := func(arg ast.Expr) string {
		literal, ok := ast.Unparen(arg).(*ast.FuncLit)
		if !ok || literal.Type.Params == nil || len(literal.Type.Params.List) == 0 || len(literal.Type.Params.List[0].Names) == 0 {
			return ""
		}
		return objectRef(info.Defs[literal.Type.Params.List[0].Names[0]])
	}
	// A named type of a library outside the module and the standard
	// library (a router), through pointers.
	library := func(t types.Type) *types.Named {
		if p, ok := t.(*types.Pointer); ok {
			t = p.Elem()
		}
		n, ok := types.Unalias(t).(*types.Named)
		if !ok || n.Obj().Pkg() == nil {
			return nil
		}
		path := n.Obj().Pkg().Path()
		if !strings.Contains(strings.Split(path, "/")[0], ".") {
			return nil
		}
		if f, _, _ := where(n.Obj().Pos()); f != "" {
			return nil
		}
		return n
	}
	// "return": a library-typed variable a module function returns (a
	// router it builds), so a router mounted from the call's result is
	// that variable.
	for _, decl := range file.Decls {
		fd, ok := decl.(*ast.FuncDecl)
		if !ok || fd.Body == nil {
			continue
		}
		fn, _ := info.Defs[fd.Name].(*types.Func)
		callee := functionRef(fn)
		if callee == "" {
			continue
		}
		ast.Inspect(fd.Body, func(node ast.Node) bool {
			switch n := node.(type) {
			case *ast.FuncLit:
				return false
			case *ast.ReturnStmt:
				if len(n.Results) == 0 || library(info.TypeOf(n.Results[0])) == nil {
					return true
				}
				if _, ok := ast.Unparen(n.Results[0]).(*ast.Ident); !ok {
					return true
				}
				if r := reference(n.Results[0]); r != "" {
					out := fact{Kind: "return", Callee: callee, Operand: r}
					out.File, out.Line, out.Column = where(n.Pos())
					emit(out)
				}
			}
			return true
		})
	}
	// The variable or field each call's result is assigned to; and, as
	// "alias" facts, a library-typed variable, field or parameter assigned
	// to another (a struct field set from a constructor's parameter).
	// The blocks that run only when a value holds an asserted type: the
	// body of "if v, ok := x.(T); ok" and a type switch case naming one
	// type, with the guard each sets.
	type guardedBlock struct {
		start, end token.Pos
		guard      guard
	}
	var guarded []guardedBlock
	assertion := func(x ast.Expr, t types.Type) (guard, bool) {
		r := reference(x)
		if r == "" || t == nil {
			return guard{}, false
		}
		if types.IsInterface(t) {
			return guard{Ref: r, Interface: true}, true
		}
		n := named(t)
		if n == nil {
			return guard{}, false
		}
		return guard{Ref: r, Type: n}, true
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.IfStmt:
			init, ok := n.Init.(*ast.AssignStmt)
			if !ok || len(init.Lhs) != 2 || len(init.Rhs) != 1 {
				return true
			}
			assert, ok := ast.Unparen(init.Rhs[0]).(*ast.TypeAssertExpr)
			okName, isIdent := init.Lhs[1].(*ast.Ident)
			cond, condIdent := ast.Unparen(n.Cond).(*ast.Ident)
			if !ok || assert.Type == nil || !isIdent || !condIdent || cond.Name != okName.Name {
				return true
			}
			if g, ok := assertion(assert.X, info.TypeOf(assert.Type)); ok {
				guarded = append(guarded, guardedBlock{n.Body.Pos(), n.Body.End(), g})
			}
		case *ast.TypeSwitchStmt:
			var x ast.Expr
			switch a := n.Assign.(type) {
			case *ast.AssignStmt:
				if len(a.Rhs) == 1 {
					if t, ok := ast.Unparen(a.Rhs[0]).(*ast.TypeAssertExpr); ok {
						x = t.X
					}
				}
			case *ast.ExprStmt:
				if t, ok := ast.Unparen(a.X).(*ast.TypeAssertExpr); ok {
					x = t.X
				}
			}
			if x == nil {
				return true
			}
			for _, s := range n.Body.List {
				clause, ok := s.(*ast.CaseClause)
				if !ok || len(clause.List) != 1 {
					continue
				}
				if id, ok := clause.List[0].(*ast.Ident); ok && id.Name == "nil" {
					continue
				}
				if g, ok := assertion(x, info.TypeOf(clause.List[0])); ok && len(clause.Body) > 0 {
					guarded = append(guarded, guardedBlock{clause.Body[0].Pos(), clause.End(), g})
				}
			}
		}
		return true
	})
	assigned := map[*ast.CallExpr]string{}
	alias := func(left string, right ast.Expr) {
		if left == "" {
			return
		}
		if _, ok := ast.Unparen(right).(*ast.CallExpr); ok {
			return
		}
		if library(info.TypeOf(right)) == nil {
			return
		}
		if r := reference(right); r != "" {
			out := fact{Kind: "alias", Assigned: left, Operand: r}
			out.File, out.Line, out.Column = where(right.Pos())
			emit(out)
		}
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.AssignStmt:
			if len(n.Lhs) == len(n.Rhs) {
				for i, right := range n.Rhs {
					if call, ok := ast.Unparen(right).(*ast.CallExpr); ok {
						assigned[call] = reference(n.Lhs[i])
					} else {
						alias(reference(n.Lhs[i]), right)
					}
				}
			} else if len(n.Rhs) == 1 {
				if call, ok := ast.Unparen(n.Rhs[0]).(*ast.CallExpr); ok {
					assigned[call] = reference(n.Lhs[0])
				}
			}
		case *ast.ValueSpec:
			for i, right := range n.Values {
				if i >= len(n.Names) {
					break
				}
				if call, ok := ast.Unparen(right).(*ast.CallExpr); ok {
					assigned[call] = objectRef(info.Defs[n.Names[i]])
				} else {
					alias(objectRef(info.Defs[n.Names[i]]), right)
				}
			}
		case *ast.KeyValueExpr:
			if key, ok := n.Key.(*ast.Ident); ok {
				if v, ok := info.Uses[key].(*types.Var); ok && v.IsField() {
					if call, ok := ast.Unparen(n.Value).(*ast.CallExpr); ok {
						assigned[call] = objectRef(v)
					} else {
						alias(objectRef(v), n.Value)
					}
				}
			}
		}
		return true
	})
	describe := func(arg ast.Expr) argument {
		a := argument{}
		if val := stringValue(arg, info, flows); val.known {
			a.Texts = val.texts
		} else if values, ok := stringsValue(arg, info, flows, literals); ok {
			a.Strings = values
		} else if selector, ok := ast.Unparen(arg).(*ast.SelectorExpr); ok {
			if id, ok := selector.X.(*ast.Ident); ok {
				if v, ok := info.Uses[id].(*types.Var); ok {
					if r, ok := results[v]; ok {
						_, line, column := where(r.call.Pos())
						a.Result = &resultField{line, column, r.index, selector.Sel.Name}
					}
				}
			}
		} else {
			// b after b, err := json.Marshal(v), through conversions
			// (sarama.ByteEncoder(b), []byte(s)): the call whose result
			// the variable holds, with no field.
			inner := ast.Unparen(arg)
			for {
				call, ok := inner.(*ast.CallExpr)
				if !ok || len(call.Args) != 1 || !info.Types[call.Fun].IsType() {
					break
				}
				inner = ast.Unparen(call.Args[0])
			}
			if id, ok := inner.(*ast.Ident); ok {
				if v, ok := info.Uses[id].(*types.Var); ok {
					if r, ok := results[v]; ok {
						_, line, column := where(r.call.Pos())
						a.Result = &resultField{line, column, r.index, ""}
					}
				}
			}
		}
		return a
	}
	ast.Inspect(file, func(node ast.Node) bool {
		if literal, ok := node.(*ast.CompositeLit); ok {
			t := info.TypeOf(literal)
			if t == nil {
				return true
			}
			if _, ok := t.Underlying().(*types.Struct); !ok {
				return true
			}
			n := named(t)
			if n == nil || n.Package == own {
				return true
			}
			out := fact{Kind: "literal", Type: n}
			out.File, out.Line, out.Column = where(literal.Pos())
			for _, e := range literal.Elts {
				kv, ok := e.(*ast.KeyValueExpr)
				if !ok {
					continue
				}
				key, ok := kv.Key.(*ast.Ident)
				if !ok {
					continue
				}
				value := kv.Value
				if unary, ok := value.(*ast.UnaryExpr); ok && unary.Op == token.AND {
					value = unary.X
				}
				out.Elements = append(out.Elements, element{key.Name, describe(value)})
			}
			if len(out.Elements) > 0 {
				emit(out)
			}
			return true
		}
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		// A call of the module's own function that passes a value of a
		// library type, or net/http's request or response writer: which
		// parameter receives it ("pass").
		if fn := called(call.Fun); fn != nil {
			if callee := functionRef(fn); callee != "" {
				params := fn.Type().(*types.Signature).Params()
				out := fact{Kind: "pass", Callee: callee}
				for i, arg := range call.Args {
					if i >= params.Len() || (library(info.TypeOf(arg)) == nil && !netHTTP(info.TypeOf(arg))) {
						continue
					}
					if r := reference(arg); r != "" {
						out.Arguments = append(out.Arguments, argument{Ref: r, Parameter: objectRef(params.At(i))})
					}
				}
				if len(out.Arguments) > 0 {
					for i, arg := range call.Args {
						if i >= params.Len() || !types.IsInterface(params.At(i).Type()) {
							continue
						}
						parameter := objectRef(params.At(i))
						t := info.TypeOf(arg)
						if parameter == "" || t == nil {
							continue
						}
						typed := typedArgument{Ref: reference(arg), Parameter: parameter}
						if types.IsInterface(t) {
							typed.Interface = true
						} else {
							typed.Type = named(t)
						}
						out.Typed = append(out.Typed, typed)
					}
					for _, block := range guarded {
						if block.start <= call.Pos() && call.Pos() < block.end {
							out.Guards = append(out.Guards, block.guard)
						}
					}
					out.File, out.Line, out.Column = where(call.Pos())
					emit(out)
				}
			} else if fn.Type().(*types.Signature).Recv() == nil && fn.Pkg() != nil {
				// A library constructor: a package function whose first
				// result is a type of its own package (gin.Default).
				results := fn.Type().(*types.Signature).Results()
				if results.Len() > 0 {
					if n := library(results.At(0).Type()); n != nil && n.Obj().Pkg() == fn.Pkg() {
						out := fact{Kind: "new", Type: named(n), Method: fn.Name(), Declared: fn.Pkg().Path(), Assigned: assigned[call], Self: reference(call)}
						out.File, out.Line, out.Column = where(call.Pos())
						emit(out)
					}
				}
			}
		}
		selector, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		// encoding/json's Marshal and MarshalIndent: the named type of the
		// value serialized, which a message whose body they make carries.
		if function, ok := info.Uses[selector.Sel].(*types.Func); ok && len(call.Args) >= 1 {
			if pkg := function.Pkg(); pkg != nil && pkg.Path() == "encoding/json" &&
				(function.Name() == "Marshal" || function.Name() == "MarshalIndent") {
				f, line, column := where(call.Pos())
				// encoding/json serializes the dynamic type of an interface
				// value, which the static type does not give.
				value := argument{Type: named(info.TypeOf(call.Args[0]))}
				if t := info.TypeOf(call.Args[0]); t != nil && types.IsInterface(t) {
					value = argument{Interface: true}
				}
				emit(fact{
					Kind: "call", File: f, Line: line, Column: column,
					Method: function.Name(), Declared: "encoding/json",
					Arguments: []argument{value},
				})
				return true
			}
		}
		// go-chi/render's package functions that decode a request body
		// into, or render a response from, their last argument.
		if function, ok := info.Uses[selector.Sel].(*types.Func); ok {
			if pkg := function.Pkg(); pkg != nil && pkg.Path() == "github.com/go-chi/render" && chiRender[function.Name()] {
				f, line, column := where(call.Pos())
				out := fact{Kind: "call", File: f, Line: line, Column: column, Method: function.Name(), Declared: pkg.Path()}
				for _, arg := range call.Args {
					a := argument{Ref: reference(arg), Type: named(info.TypeOf(arg))}
					if t := info.TypeOf(arg); t != nil && types.IsInterface(t) {
						a = argument{Ref: reference(arg), Interface: true}
					}
					out.Arguments = append(out.Arguments, a)
				}
				emit(out)
				return true
			}
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
			out.ResultTypes = append(out.ResultTypes, named(results.At(i).Type()))
		}
		_, out.NameLine, out.NameColumn = where(selector.Sel.Pos())
		for _, arg := range call.Args {
			if t := info.TypeOf(arg); t != nil {
				out.Messages = append(out.Messages, messagesIn(t, named)...)
			}
		}
		for i := 0; i < results.Len(); i++ {
			out.Messages = append(out.Messages, messagesIn(results.At(i).Type(), named)...)
		}
		out.Operand = reference(selector.X)
		out.Assigned = assigned[call]
		out.Self = reference(call)
		for i, arg := range call.Args {
			a := argument{}
			if i < 3 {
				a = describe(arg)
				a.Type = named(info.TypeOf(arg))
				if t := info.TypeOf(arg); t != nil && types.IsInterface(t) {
					a.Interface = true
				}
			}
			a.Ref = reference(arg)
			a.Function = function(arg)
			a.Literal = literalParameter(arg)
			out.Arguments = append(out.Arguments, a)
		}
		emit(out)
		return true
	})
}

// chiRender names go-chi/render's functions whose last argument is the
// request body decoded or the response rendered.
var chiRender = map[string]bool{
	"JSON": true, "Render": true, "Respond": true,
	"Bind": true, "Decode": true, "DecodeJSON": true,
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
// function with goto or labels holds, is never known. A func literal starts
// from the values its enclosing function has where the literal is, of the
// variables written only once (their declaration): those hold that value
// whenever the literal runs. It records the value each use of a variable
// reads.
type flow struct {
	info    *types.Info
	escaped map[*types.Var]bool
	writes  map[*types.Var]int
	uses    map[*ast.Ident]value
	fixed   map[*types.Var]string // see packageValues
}

// packageValues maps each package variable whose initializer is a constant
// string to that string, when nothing else can change it: no assignment,
// ++/--, range or address-of (other than as a keyed element of a composite
// literal) in the package touches it, and it is
// unexported (or the package is main, which nothing imports).
func packageValues(files []*ast.File, info *types.Info, pkg *types.Package) map[*types.Var]string {
	found := map[*types.Var]string{}
	for _, file := range files {
		for _, decl := range file.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.VAR {
				continue
			}
			for _, spec := range gen.Specs {
				vs := spec.(*ast.ValueSpec)
				if len(vs.Values) != len(vs.Names) {
					continue
				}
				for i, id := range vs.Names {
					v, ok := info.Defs[id].(*types.Var)
					if !ok || (id.IsExported() && pkg.Name() != "main") {
						continue
					}
					if c := info.Types[vs.Values[i]].Value; c != nil && c.Kind() == constant.String {
						found[v] = constant.StringVal(c)
					}
				}
			}
		}
	}
	drop := func(e ast.Expr) {
		if id, ok := ast.Unparen(e).(*ast.Ident); ok {
			if v, ok := info.Uses[id].(*types.Var); ok {
				delete(found, v)
			}
		}
	}
	stored := map[*ast.UnaryExpr]bool{}
	for _, file := range files {
		ast.Inspect(file, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.CompositeLit:
				// &x as a keyed element, as for locals (see newFlow).
				for _, e := range n.Elts {
					if kv, ok := e.(*ast.KeyValueExpr); ok {
						if unary, ok := kv.Value.(*ast.UnaryExpr); ok && unary.Op == token.AND {
							stored[unary] = true
						}
					}
				}
			case *ast.AssignStmt:
				for _, left := range n.Lhs {
					drop(left)
				}
			case *ast.IncDecStmt:
				drop(n.X)
			case *ast.RangeStmt:
				drop(n.Key)
				drop(n.Value)
			case *ast.UnaryExpr:
				if n.Op == token.AND && !stored[n] {
					drop(n.X)
				}
			}
			return true
		})
	}
	return found
}

// packageValue is the value of a package variable packageValues fixes.
func (f *flow) packageValue(id *ast.Ident) (value, bool) {
	if v, ok := f.info.Uses[id].(*types.Var); ok {
		if text, ok := f.fixed[v]; ok {
			return known(text), true
		}
	}
	return unknown, false
}

func newFlow(file *ast.File, info *types.Info) *flow {
	f := &flow{info: info, escaped: map[*types.Var]bool{}, writes: map[*types.Var]int{}, uses: map[*ast.Ident]value{}}
	written := func(e ast.Expr) {
		if id, ok := e.(*ast.Ident); ok {
			if v := f.local(id); v != nil {
				f.writes[v]++
			}
		}
	}
	// Variables a func literal assigns, or whose address is taken other
	// than as a keyed element of a composite literal
	// (kafka.TopicPartition{Topic: &topic}, read as topic's value there).
	stored := map[*ast.UnaryExpr]bool{}
	var visit func(node ast.Node, inside []*ast.FuncLit)
	visit = func(node ast.Node, inside []*ast.FuncLit) {
		ast.Inspect(node, func(n ast.Node) bool {
			switch n := n.(type) {
			case *ast.CompositeLit:
				for _, e := range n.Elts {
					if kv, ok := e.(*ast.KeyValueExpr); ok {
						if unary, ok := kv.Value.(*ast.UnaryExpr); ok && unary.Op == token.AND {
							stored[unary] = true
						}
					}
				}
			case *ast.FuncLit:
				if n != node {
					visit(n.Body, append(inside, n))
					return false
				}
			case *ast.UnaryExpr:
				if id, ok := ast.Unparen(n.X).(*ast.Ident); ok && n.Op == token.AND && !stored[n] {
					if v := f.local(id); v != nil {
						f.escaped[v] = true
					}
				}
			case *ast.ValueSpec:
				for _, id := range n.Names {
					written(id)
				}
			case *ast.RangeStmt:
				written(n.Key)
				written(n.Value)
			case *ast.IncDecStmt:
				written(n.X)
			case *ast.AssignStmt:
				for _, left := range n.Lhs {
					written(left)
				}
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

// captured is the part of a state a func literal starts from: the known
// values of variables written once.
func (f *flow) captured(st state) state {
	out := state{}
	for v, val := range st {
		if val.known && f.writes[v] == 1 && !f.escaped[v] {
			out[v] = val
		}
	}
	return out
}

// function follows one function body from the values it captures.
func (f *flow) function(body *ast.BlockStmt, captured state) {
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
		f.expressions(body, captured) // every other variable stays unknown
		return
	}
	f.block(body.List, captured.copy())
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
			f.function(n.Body, f.captured(st))
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
		if v == nil {
			return f.packageValue(id)
		}
		if f.escaped[v] {
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
		if f.local(id) == nil {
			return f.packageValue(id)
		}
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

// resultOf is the call a local variable is assigned from, and which of the
// call's results it holds.
type resultOf struct {
	call  *ast.CallExpr
	index int
}

// once maps each local variable of a file that is assigned exactly once,
// from a composite literal, to that literal; and each local assigned exactly
// once, from one result of a multi-value call, to that call. A variable
// whose address is taken (other than as a keyed element of a composite
// literal) is in neither.
func once(file ast.Node, info *types.Info) (map[*types.Var]*ast.CompositeLit, map[*types.Var]resultOf) {
	count := map[*types.Var]int{}
	literals := map[*types.Var]*ast.CompositeLit{}
	calls := map[*types.Var]resultOf{}
	escaped := map[*types.Var]bool{}
	stored := map[*ast.UnaryExpr]bool{}
	variable := func(id *ast.Ident) *types.Var {
		obj := info.Defs[id]
		if obj == nil {
			obj = info.Uses[id]
		}
		v, _ := obj.(*types.Var)
		return v
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.AssignStmt:
			for i, left := range n.Lhs {
				id, ok := left.(*ast.Ident)
				if !ok {
					continue
				}
				v := variable(id)
				if v == nil {
					continue
				}
				count[v]++
				if n.Tok != token.ASSIGN && n.Tok != token.DEFINE {
					count[v]++
					continue
				}
				if len(n.Rhs) == len(n.Lhs) {
					if literal, ok := ast.Unparen(n.Rhs[i]).(*ast.CompositeLit); ok {
						literals[v] = literal
					}
				} else if call, ok := n.Rhs[0].(*ast.CallExpr); ok && len(n.Rhs) == 1 {
					calls[v] = resultOf{call, i}
				}
			}
		case *ast.ValueSpec:
			for i, id := range n.Names {
				v := variable(id)
				if v == nil {
					continue
				}
				count[v]++
				if len(n.Values) == len(n.Names) {
					if literal, ok := ast.Unparen(n.Values[i]).(*ast.CompositeLit); ok {
						literals[v] = literal
					}
				} else if len(n.Values) == 1 {
					if call, ok := n.Values[0].(*ast.CallExpr); ok {
						calls[v] = resultOf{call, i}
					}
				}
			}
		case *ast.RangeStmt:
			for _, e := range []ast.Expr{n.Key, n.Value} {
				if id, ok := e.(*ast.Ident); ok {
					if v := variable(id); v != nil {
						count[v] += 2
					}
				}
			}
		case *ast.IncDecStmt:
			if id, ok := n.X.(*ast.Ident); ok {
				if v := variable(id); v != nil {
					count[v] += 2
				}
			}
		case *ast.CompositeLit:
			// &x as a keyed element (kafka.TopicPartition{Topic: &topic})
			// stores the pointer in a field; it is not read as a write.
			for _, e := range n.Elts {
				if kv, ok := e.(*ast.KeyValueExpr); ok {
					if unary, ok := kv.Value.(*ast.UnaryExpr); ok && unary.Op == token.AND {
						stored[unary] = true
					}
				}
			}
		case *ast.UnaryExpr:
			if id, ok := ast.Unparen(n.X).(*ast.Ident); ok && n.Op == token.AND && !stored[n] {
				if v := variable(id); v != nil {
					escaped[v] = true
				}
			}
		}
		return true
	})
	for v := range literals {
		if count[v] != 1 || escaped[v] {
			delete(literals, v)
		}
	}
	for v := range calls {
		if count[v] != 1 || escaped[v] {
			delete(calls, v)
		}
	}
	return literals, calls
}

// stringsValue is the texts of each element of a []string literal, or of
// the one such literal assigned to a local variable assigned once; nil for
// an element without one.
func stringsValue(arg ast.Expr, info *types.Info, f *flow, literals map[*types.Var]*ast.CompositeLit) ([][]string, bool) {
	arg = ast.Unparen(arg)
	if id, ok := arg.(*ast.Ident); ok {
		v, ok := info.Uses[id].(*types.Var)
		if !ok || literals[v] == nil {
			return nil, false
		}
		arg = literals[v]
	}
	literal, ok := arg.(*ast.CompositeLit)
	if !ok {
		return nil, false
	}
	t := info.TypeOf(literal)
	if t == nil {
		return nil, false
	}
	slice, ok := t.Underlying().(*types.Slice)
	if !ok {
		return nil, false
	}
	if b, ok := slice.Elem().Underlying().(*types.Basic); !ok || b.Info()&types.IsString == 0 {
		return nil, false
	}
	values := [][]string{}
	for _, e := range literal.Elts {
		if val := stringValue(e, info, f); val.known {
			values = append(values, val.texts)
		} else {
			values = append(values, nil)
		}
	}
	return values, true
}

// structs prints the fields of the struct types a file declares at package
// level: the exported ones and the embedded ones (encoding/json writes no
// other, nor one tagged json:"-").
func structs(file *ast.File, info *types.Info, emit func(fact), where func(token.Pos) (string, int, int)) {
	var holds func(t types.Type, field *structField, seen map[types.Type]bool, depth int)
	holds = func(t types.Type, field *structField, seen map[types.Type]bool, depth int) {
		if t == nil || depth > 8 || seen[t] {
			return
		}
		seen[t] = true
		switch u := types.Unalias(t).(type) {
		case *types.Pointer:
			holds(u.Elem(), field, seen, depth+1)
		case *types.Slice:
			holds(u.Elem(), field, seen, depth+1)
		case *types.Array:
			holds(u.Elem(), field, seen, depth+1)
		case *types.Map:
			holds(u.Key(), field, seen, depth+1)
			holds(u.Elem(), field, seen, depth+1)
		case *types.Chan:
			holds(u.Elem(), field, seen, depth+1)
		case *types.Interface:
			field.Interface = true
		case *types.Struct:
			for i := 0; i < u.NumFields(); i++ {
				holds(u.Field(i).Type(), field, seen, depth+1)
			}
		case *types.Named:
			args := u.TypeArgs()
			for i := 0; args != nil && i < args.Len(); i++ {
				holds(args.At(i), field, seen, depth+1)
			}
			obj := u.Obj()
			if obj.Pkg() == nil {
				return // error, any's underlying: not a declaration
			}
			f, line, _ := where(obj.Pos())
			if f == "" || strings.HasPrefix(f, "vendor/") || strings.Contains(f, "/vendor/") {
				return // a library's type
			}
			if types.IsInterface(u) {
				field.Interface = true
				return
			}
			field.Types = append(field.Types, namedType{Package: obj.Pkg().Path(), Name: obj.Name(), File: f, Line: line})
			// A named slice, map, array, pointer or channel (type Labels
			// []Label) holds what its underlying type holds; a struct's
			// fields are printed with the struct itself.
			if _, ok := u.Underlying().(*types.Struct); !ok {
				holds(u.Underlying(), field, seen, depth+1)
			}
		}
	}
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.TYPE {
			continue
		}
		for _, spec := range gen.Specs {
			ts, ok := spec.(*ast.TypeSpec)
			if !ok {
				continue
			}
			obj, ok := info.Defs[ts.Name].(*types.TypeName)
			if !ok || obj.IsAlias() {
				continue
			}
			st, ok := obj.Type().Underlying().(*types.Struct)
			if !ok {
				continue
			}
			out := fact{Kind: "struct", Type: &namedType{Package: obj.Pkg().Path(), Name: obj.Name()}}
			out.File, out.Line, _ = where(ts.Name.Pos())
			for i := 0; i < st.NumFields(); i++ {
				v := st.Field(i)
				if !v.Exported() && !v.Embedded() {
					continue
				}
				// json:"-" omits the field; json:"-," names it "-".
				if reflect.StructTag(st.Tag(i)).Get("json") == "-" {
					continue
				}
				_, line, _ := where(v.Pos())
				field := structField{Name: v.Name(), Line: line, Embedded: v.Embedded()}
				holds(v.Type(), &field, map[types.Type]bool{}, 0)
				if len(field.Types) > 0 || field.Interface {
					out.Fields = append(out.Fields, field)
				}
			}
			if len(out.Fields) > 0 {
				emit(out)
			}
		}
	}
}
