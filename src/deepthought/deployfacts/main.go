// Command deployfacts renders the deployment descriptions of a repository
// with the tools that define them and prints, as JSON lines, what they
// declare:
//
//   - "compose": a Compose project as docker compose loads it
//     (compose-go): the files combined in order (a file and its override),
//     extends, include and ${VARIABLE} interpolation from the project's .env
//     file. The process environment is never read: a variable that neither
//     .env nor a default sets is written as Placeholder and listed under
//     "missing".
//   - "manifests": the YAML documents `helm template` renders for a chart
//     (after `helm dependency build` for file:// dependencies) with its own
//     default values (no cluster, release name
//     "release-name"), or that `kustomize build` renders for a
//     kustomization directory (no plugins, no Helm inflation, loading
//     restricted to the directory's tree).
//   - "error": a description the tool could not render, with its message.
//
// Requests are read from stdin, one JSON object a line:
// {"kind": "compose", "directory": ..., "files": [every Compose file there]},
// {"kind": "helm", "directory": ...} or {"kind": "kustomize", "directory": ...},
// paths relative to ROOT. Nothing is built, pulled or run.
//
// Usage: deployfacts ROOT < requests
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/Masterminds/semver/v3"
	composecli "github.com/compose-spec/compose-go/v2/cli"
	"github.com/compose-spec/compose-go/v2/dotenv"
	composeloader "github.com/compose-spec/compose-go/v2/loader"
	"github.com/compose-spec/compose-go/v2/template"
	composetypes "github.com/compose-spec/compose-go/v2/types"
	"github.com/sirupsen/logrus"
	"helm.sh/helm/v3/pkg/action"
	helmchart "helm.sh/helm/v3/pkg/chart"
	chartloader "helm.sh/helm/v3/pkg/chart/loader"
	"helm.sh/helm/v3/pkg/chartutil"
	"sigs.k8s.io/kustomize/api/krusty"
	kusttypes "sigs.k8s.io/kustomize/api/types"
	"sigs.k8s.io/kustomize/kyaml/filesys"
)

// Placeholder stands for text the description does not fix. It is a
// private-use character, not NUL: compose-go's volume parser reads NUL as
// the end of a spec.
const Placeholder = "\uE000"

var variable = regexp.MustCompile(`^\$\{?([A-Za-z_][A-Za-z0-9_]*)`)

type request struct {
	Kind      string   `json:"kind"`
	Directory string   `json:"directory"`
	Files     []string `json:"files,omitempty"`
}

type volume struct {
	Type   string `json:"type"`             // bind, volume, tmpfs, ...
	Source string `json:"source,omitempty"` // a bind's: relative to ROOT when inside it
	Target string `json:"target"`
}

type service struct {
	Name       string   `json:"name"`
	Image      string   `json:"image,omitempty"`
	Context    *string  `json:"context,omitempty"` // relative to ROOT; "" is ROOT
	Dockerfile *string  `json:"dockerfile,omitempty"`
	DependsOn  []string `json:"depends_on,omitempty"`
	// Environment as interpolated; null for a name without a value.
	Environment map[string]*string `json:"environment,omitempty"`
	Volumes     []volume           `json:"volumes,omitempty"`
	Profiles    []string           `json:"profiles,omitempty"`
}

type fact struct {
	Kind     string    `json:"kind"`
	Request  request   `json:"request"`
	Files    []string  `json:"files,omitempty"` // files the project read
	Services []service `json:"services,omitempty"`
	Missing  []string  `json:"missing,omitempty"`
	// An env_file was missing, so env_file entries were not applied.
	UnreadEnvFiles bool   `json:"unread_env_files,omitempty"`
	Manifests      string `json:"manifests,omitempty"`
	// Files kustomize read to render the kustomization, relative to ROOT.
	Inputs  []string `json:"inputs,omitempty"`
	Message string   `json:"message,omitempty"`
}

var root string

func emit(f fact) {
	out, _ := json.Marshal(f)
	fmt.Println(string(out))
}

// inside is a path relative to ROOT, or false for one outside it.
func inside(path string) (string, bool) {
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return "", false
	}
	if rel == "." {
		return "", true
	}
	return filepath.ToSlash(rel), true
}

func relative(path string) string {
	if rel, ok := inside(path); ok {
		return rel
	}
	return path
}

func compose(r request) fact {
	directory := filepath.Join(root, r.Directory)
	environment := map[string]string{}
	if env := filepath.Join(directory, ".env"); fileExists(env) {
		values, err := dotenv.GetEnvFromFile(map[string]string{}, []string{env})
		if err != nil {
			return fact{Kind: "error", Request: r, Message: err.Error()}
		}
		environment = values
	}
	if _, ok := environment["PWD"]; !ok {
		// docker compose is run from the project directory.
		environment["PWD"] = directory
	}
	missing := map[string]bool{}
	files := []composetypes.ConfigFile{}
	for _, f := range r.Files {
		files = append(files, composetypes.ConfigFile{Filename: filepath.Join(root, f)})
	}
	details := composetypes.ConfigDetails{WorkingDir: directory, ConfigFiles: files, Environment: environment}
	load := func(envFiles bool) (*composetypes.Project, error) {
		return composeloader.LoadWithContext(context.Background(), details, func(o *composeloader.Options) {
			o.SetProjectName("project", true)
			o.SkipConsistencyCheck = true
			o.SkipResolveEnvironment = !envFiles
			o.Profiles = []string{"*"}
			o.Interpolate.LookupValue = func(key string) (string, bool) {
				value, ok := environment[key]
				return value, ok
			}
			// The loader's own substitution (defaults, :?, nesting), with a
			// variable nothing sets written as Placeholder, not "".
			replace := func(text string, mapping template.Mapping, cfg *template.Config) (string, error) {
				value, ok, err := template.DefaultReplacementAppliedFunc(text, mapping, cfg)
				if err != nil || ok {
					return value, err
				}
				if name := variable.FindStringSubmatch(text); name != nil {
					missing[name[1]] = true
				}
				return Placeholder, nil
			}
			o.Interpolate.Substitute = func(text string, mapping template.Mapping) (string, error) {
				return template.SubstituteWithOptions(text, mapping, template.WithReplacementFunction(replace), template.WithoutLogging)
			}
		})
	}
	project, err := load(true)
	unreadEnvFiles := false
	if err != nil && strings.Contains(err.Error(), "env file") {
		// An env_file the checkout lacks (often git-ignored): the services'
		// own environment is still the project's.
		project, err = load(false)
		unreadEnvFiles = true
	}
	if err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	out := fact{Kind: "compose", Request: r, UnreadEnvFiles: unreadEnvFiles}
	for _, f := range project.ComposeFiles {
		out.Files = append(out.Files, relative(f))
	}
	names := []string{}
	for name := range project.Services {
		names = append(names, name)
	}
	for name := range project.DisabledServices {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		s, ok := project.Services[name]
		if !ok {
			s = project.DisabledServices[name]
		}
		item := service{Name: name, Image: s.Image, Profiles: s.Profiles}
		// A context holding text no variable sets builds no known directory.
		if s.Build != nil && s.Build.Context != "" && filepath.IsAbs(s.Build.Context) && !strings.Contains(s.Build.Context, Placeholder) {
			if context, ok := inside(s.Build.Context); ok {
				item.Context = &context
				if s.Build.Dockerfile != "" && !strings.Contains(s.Build.Dockerfile, Placeholder) {
					dockerfile := s.Build.Dockerfile
					if !filepath.IsAbs(dockerfile) {
						dockerfile = filepath.Join(s.Build.Context, dockerfile)
					}
					if rel, ok := inside(dockerfile); ok {
						item.Dockerfile = &rel
					}
				}
			}
		}
		for _, v := range s.Volumes {
			mount := volume{Type: v.Type, Target: v.Target}
			if v.Type == composetypes.VolumeTypeBind {
				mount.Source = relative(v.Source)
			}
			item.Volumes = append(item.Volumes, mount)
		}
		for dependency := range s.DependsOn {
			item.DependsOn = append(item.DependsOn, dependency)
		}
		sort.Strings(item.DependsOn)
		if len(s.Environment) > 0 {
			item.Environment = map[string]*string{}
			for k, v := range s.Environment {
				item.Environment[k] = v
			}
		}
		out.Services = append(out.Services, item)
	}
	for key := range missing {
		out.Missing = append(out.Missing, key)
	}
	sort.Strings(out.Missing)
	return out
}

// composeDirectory loads the Compose files of one directory as docker
// compose does: the default file it discovers there, with its override file,
// is one project (compose-go's WithDefaultConfigPath, which `docker compose`
// runs without -f), and every other file is a project of its own, as
// `docker compose -f FILE` reads it.
func composeDirectory(r request) []fact {
	directory := filepath.Join(root, r.Directory)
	used := map[string]bool{}
	out := []fact{}
	options, err := composecli.NewProjectOptions(nil, composecli.WithWorkingDirectory(directory), composecli.WithDefaultConfigPath)
	// Discovery walks up to parent directories when this one has no default
	// file; those projects are their own directory's.
	if err == nil && len(options.ConfigPaths) > 0 && filepath.Dir(options.ConfigPaths[0]) == directory {
		files := []string{}
		for _, path := range options.ConfigPaths {
			rel := relative(path)
			files = append(files, rel)
			used[rel] = true
		}
		out = append(out, compose(request{Kind: "compose", Directory: r.Directory, Files: files}))
	}
	for _, file := range r.Files {
		if !used[file] {
			out = append(out, compose(request{Kind: "compose", Directory: r.Directory, Files: []string{file}}))
		}
	}
	return out
}

// localDependencies adds the dependencies a chart takes from file://
// directories and does not already hold under charts/, as `helm dependency
// build` archives them: the directory relative to the chart (or absolute),
// loaded as it is, at a version the constraint accepts. Only directories
// inside ROOT are read.
func localDependencies(chart *helmchart.Chart, directory string) error {
	held := map[string]bool{}
	for _, dependency := range chart.Dependencies() {
		held[dependency.Name()] = true
	}
	for _, dependency := range chart.Metadata.Dependencies {
		if held[dependency.Name] || !strings.HasPrefix(dependency.Repository, "file://") {
			continue
		}
		path := strings.TrimPrefix(dependency.Repository, "file://")
		if !filepath.IsAbs(path) {
			path = filepath.Join(directory, path)
		}
		if _, ok := inside(path); !ok {
			return fmt.Errorf("dependency %s is outside the repository: %s", dependency.Name, dependency.Repository)
		}
		local, err := chartloader.LoadDir(path)
		if err != nil {
			return fmt.Errorf("dependency %s: %w", dependency.Name, err)
		}
		if dependency.Version != "" {
			constraint, err := semver.NewConstraint(dependency.Version)
			if err != nil {
				return fmt.Errorf("dependency %s has an invalid version/constraint format: %w", dependency.Name, err)
			}
			version, err := semver.NewVersion(local.Metadata.Version)
			if err != nil {
				return err
			}
			if !constraint.Check(version) {
				return fmt.Errorf("can't get a valid version for dependency %s", dependency.Name)
			}
		}
		chart.AddDependency(local)
		held[dependency.Name] = true
	}
	return nil
}

func helm(r request) fact {
	directory := filepath.Join(root, r.Directory)
	chart, err := chartloader.Load(directory)
	if err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	if err := localDependencies(chart, directory); err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	if err := action.CheckDependencies(chart, chart.Metadata.Dependencies); err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	install := action.NewInstall(&action.Configuration{})
	install.DryRun = true
	install.ClientOnly = true
	install.Replace = true
	install.IncludeCRDs = true
	install.ReleaseName = "release-name"
	install.Namespace = "default"
	install.KubeVersion = &chartutil.DefaultCapabilities.KubeVersion
	release, err := install.Run(chart, map[string]interface{}{})
	if err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	return fact{Kind: "manifests", Request: r, Manifests: release.Manifest}
}

// recording is the disk, noting every file read through it.
type recording struct {
	filesys.FileSystem
	read map[string]bool
}

func (fs recording) ReadFile(path string) ([]byte, error) {
	data, err := fs.FileSystem.ReadFile(path)
	if err == nil {
		fs.read[path] = true
	}
	return data, err
}

func (fs recording) Open(path string) (filesys.File, error) {
	file, err := fs.FileSystem.Open(path)
	if err == nil {
		fs.read[path] = true
	}
	return file, err
}

func kustomize(r request) fact {
	options := krusty.MakeDefaultOptions()
	options.LoadRestrictions = kusttypes.LoadRestrictionsRootOnly
	options.PluginConfig = kusttypes.DisabledPluginConfig()
	disk := recording{filesys.MakeFsOnDisk(), map[string]bool{}}
	resources, err := krusty.MakeKustomizer(options).Run(disk, filepath.Join(root, r.Directory))
	if err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	text, err := resources.AsYaml()
	if err != nil {
		return fact{Kind: "error", Request: r, Message: err.Error()}
	}
	out := fact{Kind: "manifests", Request: r, Manifests: string(text)}
	for path := range disk.read {
		abs, err := filepath.Abs(path)
		if err != nil {
			continue
		}
		if rel, ok := inside(abs); ok {
			out.Inputs = append(out.Inputs, rel)
		}
	}
	sort.Strings(out.Inputs)
	return out
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func main() {
	log.SetOutput(io.Discard)
	logrus.SetOutput(io.Discard)
	root, _ = filepath.Abs(os.Args[1])
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1<<20), 1<<24)
	for scanner.Scan() {
		var r request
		if err := json.Unmarshal(scanner.Bytes(), &r); err != nil {
			emit(fact{Kind: "error", Message: err.Error()})
			continue
		}
		switch r.Kind {
		case "compose":
			for _, f := range composeDirectory(r) {
				emit(f)
			}
		case "helm":
			emit(helm(r))
		case "kustomize":
			emit(kustomize(r))
		default:
			emit(fact{Kind: "error", Request: r, Message: "unknown kind " + r.Kind})
		}
	}
}
