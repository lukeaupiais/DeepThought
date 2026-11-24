// Command go_std_interfaces writes the method sets of the standard library's
// exported interfaces as JSON, for deepthought.go_interfaces: an interface
// of the analyzed files that embeds one (io.Reader, fmt.Stringer) has its
// methods too, and a type implements it only with all of them.
//
// It reads package paths on stdin, type-checks each from source with the
// standard library's go/types and lists every exported, non-generic
// interface type whose methods are all exported and whose signatures it can
// write as Definition.signature does, with each named type written as
// "import/path".Name, byte and rune as uint8 and int32, and any as
// interface{}. Other interfaces are left out. It builds and runs nothing.
//
// Usage:
//
//	go list std | grep -v -e internal -e vendor |
//	  go run tools/go_std_interfaces.go > src/deepthought/go_std_interfaces.json
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"go/importer"
	"go/token"
	"go/types"
	"os"
	"runtime"
	"sort"
	"strings"
)

func write(t types.Type) (string, bool) {
	t = types.Unalias(t)
	switch t := t.(type) {
	case *types.Basic:
		switch t.Kind() {
		case types.Bool, types.Int, types.Int8, types.Int16, types.Int32, types.Int64,
			types.Uint, types.Uint8, types.Uint16, types.Uint32, types.Uint64, types.Uintptr,
			types.Float32, types.Float64, types.Complex64, types.Complex128, types.String:
			return types.Typ[t.Kind()].Name(), true
		}
		return "", false
	case *types.Named:
		if t.TypeArgs().Len() > 0 || t.TypeParams().Len() > 0 {
			return "", false
		}
		obj := t.Obj()
		if obj.Pkg() == nil {
			if obj.Name() == "error" {
				return "error", true
			}
			return "", false
		}
		return fmt.Sprintf("%q.%s", obj.Pkg().Path(), obj.Name()), true
	case *types.Pointer:
		elem, ok := write(t.Elem())
		return "*" + elem, ok
	case *types.Slice:
		elem, ok := write(t.Elem())
		return "[]" + elem, ok
	case *types.Array:
		elem, ok := write(t.Elem())
		return fmt.Sprintf("[%d]%s", t.Len(), elem), ok
	case *types.Map:
		key, ok := write(t.Key())
		value, ok2 := write(t.Elem())
		return "map[" + key + "]" + value, ok && ok2
	case *types.Chan:
		elem, ok := write(t.Elem())
		prefix := map[types.ChanDir]string{
			types.SendRecv: "chan ", types.SendOnly: "chan<- ", types.RecvOnly: "<-chan ",
		}[t.Dir()]
		return prefix + elem, ok
	case *types.Signature:
		sig, ok := signature(t)
		return "func" + sig, ok
	case *types.Interface:
		if t.NumMethods() == 0 && t.NumEmbeddeds() == 0 {
			return "interface{}", true
		}
	}
	return "", false
}

func tuple(vars *types.Tuple, variadic bool) (string, bool) {
	parts := []string{}
	for i := 0; i < vars.Len(); i++ {
		t := vars.At(i).Type()
		prefix := ""
		if variadic && i == vars.Len()-1 {
			t, prefix = t.(*types.Slice).Elem(), "..."
		}
		written, ok := write(t)
		if !ok {
			return "", false
		}
		parts = append(parts, prefix+written)
	}
	return "(" + strings.Join(parts, ",") + ")", true
}

func signature(sig *types.Signature) (string, bool) {
	if sig.TypeParams().Len() > 0 {
		return "", false
	}
	params, ok := tuple(sig.Params(), sig.Variadic())
	results, ok2 := tuple(sig.Results(), false)
	return params + results, ok && ok2
}

// encode writes JSON without escaping <, > and &, so "<-chan T" reads as is.
func encode(value any) string {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		panic(err)
	}
	return strings.TrimSuffix(buffer.String(), "\n")
}

func main() {
	fset := token.NewFileSet()
	source := importer.ForCompiler(fset, "source", nil)
	interfaces := map[string]map[string]map[string]string{}
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		path := strings.TrimSpace(scanner.Text())
		if path == "" {
			continue
		}
		pkg, err := source.Import(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", path, err)
			continue
		}
		scope := pkg.Scope()
		for _, name := range scope.Names() {
			obj, ok := scope.Lookup(name).(*types.TypeName)
			if !ok || !obj.Exported() || obj.IsAlias() {
				continue
			}
			named, ok := obj.Type().(*types.Named)
			if !ok || named.TypeParams().Len() > 0 {
				continue
			}
			iface, ok := named.Underlying().(*types.Interface)
			if !ok || iface.NumMethods() == 0 || !iface.IsMethodSet() {
				continue
			}
			methods := map[string]string{}
			for i := 0; i < iface.NumMethods(); i++ {
				m := iface.Method(i)
				sig, ok := signature(m.Type().(*types.Signature))
				if !m.Exported() || !ok {
					methods = nil
					break
				}
				methods[m.Name()] = sig
			}
			if methods == nil {
				continue
			}
			if interfaces[path] == nil {
				interfaces[path] = map[string]map[string]string{}
			}
			interfaces[path][name] = methods
		}
	}
	paths := []string{}
	for path := range interfaces {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	// One package per line keeps the file diffable; encoding/json sorts keys.
	fmt.Printf("{\n  \"go\": %q,\n  \"interfaces\": {\n", runtime.Version())
	for i, path := range paths {
		data, key := encode(interfaces[path]), encode(path)
		sep := ","
		if i == len(paths)-1 {
			sep = ""
		}
		fmt.Printf("    %s: %s%s\n", key, data, sep)
	}
	fmt.Println("  }\n}")
}
