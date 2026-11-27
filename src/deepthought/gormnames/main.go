// Default GORM table and column names and field tag settings, computed by
// GORM's own naming and tag-parsing code.
//
// Built inside a copy of the analyzed module's go.mod and go.sum, so
// gorm.io/gorm and github.com/jinzhu/inflection are the versions that
// module selects. Nothing of the module itself is compiled or run.
//
// stdin: {"tables": [type name, ...], "fields": [[struct tag, field name], ...]}.
// stdout: {"tables": {type name: schema.NamingStrategy{}.TableName(name)},
// "fields": [field, ...]} with, per requested field in order, the settings
// schema.ParseTagSetting reads from its `gorm` tag: the column (the COLUMN
// setting, else schema.NamingStrategy{}.ColumnName("", name)), the
// EMBEDDEDPREFIX, and whether the tag ignores the field ("-" or "-:all"),
// embeds it (EMBEDDED) or gives it a data type (TYPE or SERIALIZER).
package main

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"

	"gorm.io/gorm/schema"
)

type request struct {
	Tables []string    `json:"tables"`
	Fields [][2]string `json:"fields"`
}

type field struct {
	Column   string `json:"column"`
	Prefix   string `json:"prefix,omitempty"`
	Ignored  bool   `json:"ignored,omitempty"`
	Embedded bool   `json:"embedded,omitempty"`
	Typed    bool   `json:"typed,omitempty"`
}

type response struct {
	Tables map[string]string `json:"tables"`
	Fields []field           `json:"fields"`
}

func main() {
	var in request
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		os.Stderr.WriteString(err.Error() + "\n")
		os.Exit(2)
	}
	naming := schema.NamingStrategy{}
	out := response{Tables: make(map[string]string, len(in.Tables)), Fields: []field{}}
	for _, name := range in.Tables {
		out.Tables[name] = naming.TableName(name)
	}
	for _, pair := range in.Fields {
		settings := schema.ParseTagSetting(reflect.StructTag(pair[0]).Get("gorm"), ";")
		f := field{Column: settings["COLUMN"], Prefix: settings["EMBEDDEDPREFIX"]}
		if f.Column == "" {
			f.Column = naming.ColumnName("", pair[1])
		}
		if value, ok := settings["-"]; ok {
			value = strings.ToLower(strings.TrimSpace(value))
			f.Ignored = value == "-" || value == "all"
		}
		_, f.Embedded = settings["EMBEDDED"]
		_, typed := settings["TYPE"]
		_, serialized := settings["SERIALIZER"]
		f.Typed = typed || serialized
		out.Fields = append(out.Fields, f)
	}
	if err := json.NewEncoder(os.Stdout).Encode(out); err != nil {
		os.Stderr.WriteString(err.Error() + "\n")
		os.Exit(2)
	}
}
