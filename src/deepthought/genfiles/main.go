// Command genfiles says which files of a repository are generated, by
// go-enry's port of GitHub Linguist's rules (enry.IsGenerated): generated
// headers such as "Code generated ... DO NOT EDIT.", protobuf, gRPC, Thrift
// and other generator outputs, minified and bundled JavaScript, source maps,
// lock files and the like.
//
// It reads a JSON list of paths relative to ROOT on stdin and prints a JSON
// object mapping each path to true when it is generated. A path it cannot
// read maps to an error message instead.
//
// Usage: genfiles ROOT < paths.json
package main

import (
	"encoding/json"
	"os"
	"path/filepath"

	enry "github.com/go-enry/go-enry/v2"
)

func main() {
	root := os.Args[1]
	var paths []string
	if err := json.NewDecoder(os.Stdin).Decode(&paths); err != nil {
		panic(err)
	}
	result := map[string]any{}
	for _, path := range paths {
		content, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(path)))
		if err != nil {
			result[path] = err.Error()
			continue
		}
		result[path] = enry.IsGenerated(path, content)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
