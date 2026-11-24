// Command go_function_spans prints the span of every function and method
// declared in the .go files under ROOT, using the standard library's parser:
// one tab-separated row per declaration with the file (relative to ROOT), the
// line of its name, and its first and last lines. tools/impact_truth.py reads
// these for indexes that record no definition spans (scip-go).
//
// Usage: go run tools/go_function_spans.go ROOT > spans.tsv
package main

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func main() {
	root := os.Args[1]
	var rows []string
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() && entry.Name() == ".git" {
			return filepath.SkipDir
		}
		if entry.IsDir() || !strings.HasSuffix(path, ".go") {
			return nil
		}
		files := token.NewFileSet()
		file, err := parser.ParseFile(files, path, nil, parser.SkipObjectResolution)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: unparsed\n", path)
			return nil
		}
		relative, _ := filepath.Rel(root, path)
		for _, decl := range file.Decls {
			if function, ok := decl.(*ast.FuncDecl); ok {
				rows = append(rows, fmt.Sprintf("%s\t%d\t%d\t%d",
					filepath.ToSlash(relative),
					files.Position(function.Name.Pos()).Line,
					files.Position(function.Pos()).Line,
					files.Position(function.End()).Line))
			}
		}
		return nil
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	sort.Strings(rows)
	fmt.Println("# file\tname_line\tfirst_line\tlast_line")
	for _, row := range rows {
		fmt.Println(row)
	}
}
