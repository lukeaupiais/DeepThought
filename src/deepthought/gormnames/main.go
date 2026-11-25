// Default GORM table names, computed by GORM's own naming code.
//
// Built inside a copy of the analyzed module's go.mod and go.sum, so
// gorm.io/gorm and github.com/jinzhu/inflection are the versions that
// module selects. Nothing of the module itself is compiled or run.
//
// stdin: a JSON array of Go type names.
// stdout: a JSON object mapping each name to
// schema.NamingStrategy{}.TableName(name), GORM's default table name.
package main

import (
	"encoding/json"
	"os"

	"gorm.io/gorm/schema"
)

func main() {
	var names []string
	if err := json.NewDecoder(os.Stdin).Decode(&names); err != nil {
		os.Stderr.WriteString(err.Error() + "\n")
		os.Exit(2)
	}
	naming := schema.NamingStrategy{}
	tables := make(map[string]string, len(names))
	for _, name := range names {
		tables[name] = naming.TableName(name)
	}
	if err := json.NewEncoder(os.Stdout).Encode(tables); err != nil {
		os.Stderr.WriteString(err.Error() + "\n")
		os.Exit(2)
	}
}
