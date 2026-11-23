// Kubernetes selector oracle: decodes YAML files with the Kubernetes API types
// and prints Service -> workload selections (same namespace and directory) as
// JSON lines, using apimachinery's label selector matching.
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/serializer"
	utilyaml "k8s.io/apimachinery/pkg/util/yaml"
	"k8s.io/client-go/kubernetes/scheme"
)

type object struct {
	file, kind, namespace, name string
	labels, selector                map[string]string
}

func main() {
	decoder := serializer.NewCodecFactory(scheme.Scheme).UniversalDeserializer()
	var objects []object
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		file := in.Text()
		data, err := os.ReadFile(file)
		if err != nil {
			continue
		}
		reader := utilyaml.NewYAMLReader(bufio.NewReader(bytes.NewReader(data)))
		for {
			doc, err := reader.Read()
			if err == io.EOF || err != nil {
				break
			}
			obj, _, err := decoder.Decode(doc, nil, nil)
			if err != nil {
				continue
			}
			objects = append(objects, describe(file, obj)...)
		}
	}
	for _, s := range objects {
		if s.kind != "Service" || len(s.selector) == 0 {
			continue
		}
		selector := labels.SelectorFromSet(s.selector)
		for _, w := range objects {
			if w.kind == "Service" || w.namespace != s.namespace ||
				filepath.Dir(w.file) != filepath.Dir(s.file) {
				continue
			}
			if selector.Matches(labels.Set(w.labels)) {
				line, _ := json.Marshal([]string{s.file, s.kind + name(s), w.file, w.kind + name(w)})
				fmt.Println(string(line))
			}
		}
	}
}

func name(o object) string {
	if o.namespace != "" {
		return "/" + o.namespace + "/" + o.name
	}
	return "/" + o.name
}

func describe(file string, obj runtime.Object) []object {
	switch o := obj.(type) {
	case *corev1.Service:
		return []object{{file, "Service", o.Namespace, o.Name, nil, o.Spec.Selector}}
	case *corev1.Pod:
		return []object{{file, "Pod", o.Namespace, o.Name, o.Labels, nil}}
	case *appsv1.Deployment:
		return []object{{file, "Deployment", o.Namespace, o.Name, o.Spec.Template.Labels, nil}}
	case *appsv1.StatefulSet:
		return []object{{file, "StatefulSet", o.Namespace, o.Name, o.Spec.Template.Labels, nil}}
	case *appsv1.DaemonSet:
		return []object{{file, "DaemonSet", o.Namespace, o.Name, o.Spec.Template.Labels, nil}}
	case *appsv1.ReplicaSet:
		return []object{{file, "ReplicaSet", o.Namespace, o.Name, o.Spec.Template.Labels, nil}}
	case *batchv1.Job:
		return []object{{file, "Job", o.Namespace, o.Name, o.Spec.Template.Labels, nil}}
	case *batchv1.CronJob:
		return []object{{file, "CronJob", o.Namespace, o.Name, o.Spec.JobTemplate.Spec.Template.Labels, nil}}
	case *corev1.List:
		var found []object
		decoder := serializer.NewCodecFactory(scheme.Scheme).UniversalDeserializer()
		for _, item := range o.Items {
			inner, _, err := decoder.Decode(item.Raw, nil, nil)
			if err == nil {
				found = append(found, describe(file, inner)...)
			}
		}
		return found
	}
	return nil
}
