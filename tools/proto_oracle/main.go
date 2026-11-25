// Command proto_oracle lists the services, rpcs and google.api.http
// bindings of .proto files with github.com/emicklei/proto, a parser that
// shares no code with protoc, for tools/proto_truth.py. Build it with
// `go build` in tools/proto_oracle.
//
//	proto_oracle ROOT < paths.json > declarations.json
//
// The input is a JSON list of repository-relative .proto paths. Each file
// is parsed on its own (imports are not followed: an rpc's name, streaming
// kinds and options are all in the file that declares it).
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"

	"github.com/emicklei/proto"
)

type binding struct {
	Verb string `json:"verb"`
	Path string `json:"path"`
	Line int    `json:"line"`
}

type rpc struct {
	Service   string    `json:"service"`
	Method    string    `json:"method"`
	Line      int       `json:"line"`
	Streaming string    `json:"streaming"`
	Request   string    `json:"request"`
	Response  string    `json:"response"`
	HTTP      []binding `json:"http"`
}

type file struct {
	File    string `json:"file"`
	Package string `json:"package"`
	Error   string `json:"error,omitempty"`
	RPCs    []rpc  `json:"rpcs"`
}

var verbs = map[string]bool{"get": true, "put": true, "post": true, "delete": true, "patch": true}

func streaming(client, server bool) string {
	switch {
	case client && server:
		return "bidi"
	case client:
		return "client"
	case server:
		return "server"
	}
	return "unary"
}

// rules reads one HttpRule literal and its additional_bindings.
func rules(lit *proto.Literal, line int, out *[]binding) {
	if lit == nil {
		return
	}
	for _, field := range lit.OrderedMap {
		name := field.Name
		value := field.Literal
		switch {
		case verbs[name] && value != nil:
			*out = append(*out, binding{strings.ToUpper(name), value.Source, value.Position.Line})
		case name == "custom" && value != nil:
			kind, path := "", ""
			for _, f := range value.OrderedMap {
				if f.Name == "kind" && f.Literal != nil {
					kind = f.Literal.Source
				}
				if f.Name == "path" && f.Literal != nil {
					path = f.Literal.Source
				}
			}
			*out = append(*out, binding{kind, path, value.Position.Line})
		case name == "additional_bindings" && value != nil:
			if len(value.Array) > 0 {
				for _, item := range value.Array {
					rules(item, line, out)
				}
			} else {
				rules(value, line, out)
			}
		}
	}
}

func main() {
	root := os.Args[1]
	var paths []string
	if err := json.NewDecoder(os.Stdin).Decode(&paths); err != nil {
		panic(err)
	}
	result := []file{}
	for _, path := range paths {
		f := file{File: path, RPCs: []rpc{}}
		reader, err := os.Open(filepath.Join(root, path))
		if err != nil {
			f.Error = err.Error()
			result = append(result, f)
			continue
		}
		parsed, err := proto.NewParser(reader).Parse()
		reader.Close()
		if err != nil {
			f.Error = err.Error()
			result = append(result, f)
			continue
		}
		proto.Walk(parsed, proto.WithPackage(func(p *proto.Package) { f.Package = p.Name }))
		proto.Walk(parsed, proto.WithService(func(s *proto.Service) {
			for _, element := range s.Elements {
				r, ok := element.(*proto.RPC)
				if !ok {
					continue
				}
				item := rpc{
					Service:   s.Name,
					Method:    r.Name,
					Line:      r.Position.Line,
					Streaming: streaming(r.StreamsRequest, r.StreamsReturns),
					Request:   r.RequestType,
					Response:  r.ReturnsType,
					HTTP:      []binding{},
				}
				for _, e := range r.Elements {
					o, ok := e.(*proto.Option)
					if !ok {
						continue
					}
					if o.Name == "(google.api.http)" {
						rules(&o.Constant, o.Position.Line, &item.HTTP)
					} else if verb := strings.TrimPrefix(o.Name, "(google.api.http)."); verb != o.Name && verbs[verb] {
						// option (google.api.http).post = "/path";
						item.HTTP = append(item.HTTP, binding{strings.ToUpper(verb), o.Constant.Source, o.Position.Line})
					}
				}
				f.RPCs = append(f.RPCs, item)
			}
		}))
		result = append(result, f)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
