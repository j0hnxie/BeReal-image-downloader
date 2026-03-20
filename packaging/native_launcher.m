#import <Foundation/Foundation.h>
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static NSString *findFirstMatchingDirectory(NSString *rootPath) {
    NSFileManager *fileManager = [NSFileManager defaultManager];
    NSDirectoryEnumerator *enumerator = [fileManager enumeratorAtPath:rootPath];
    NSString *relativePath = nil;
    while ((relativePath = [enumerator nextObject])) {
        NSString *last = [relativePath lastPathComponent];
        if ([last isEqualToString:@".dylibs"] || [last isEqualToString:@"pillow.libs"]) {
            return [rootPath stringByAppendingPathComponent:relativePath];
        }
    }
    return nil;
}

static void prependEnvPath(const char *key, NSString *pathValue) {
    if (pathValue == nil || [pathValue length] == 0) {
        return;
    }

    const char *existing = getenv(key);
    NSString *newValue = pathValue;
    if (existing != NULL && existing[0] != '\0') {
        newValue = [NSString stringWithFormat:@"%@:%s", pathValue, existing];
    }
    setenv(key, [newValue UTF8String], 1);
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        NSBundle *bundle = [NSBundle mainBundle];
        NSString *resourcesPath = [bundle resourcePath];
        if (resourcesPath == nil) {
            fprintf(stderr, "Missing app resources path\n");
            return 1;
        }

        NSString *venvPath = [resourcesPath stringByAppendingPathComponent:@"venv"];
        NSString *venvPython = [venvPath stringByAppendingPathComponent:@"bin/python3"];
        NSString *scriptPath = [resourcesPath stringByAppendingPathComponent:@"app/bereal_downloader_app.py"];
        NSString *pillowDylibs = findFirstMatchingDirectory([venvPath stringByAppendingPathComponent:@"lib"]);

        if (![[NSFileManager defaultManager] isExecutableFileAtPath:venvPython]) {
            fprintf(stderr, "Bundled Python runtime not found: %s\n", [venvPython UTF8String]);
            return 1;
        }
        if (![[NSFileManager defaultManager] fileExistsAtPath:scriptPath]) {
            fprintf(stderr, "Application script not found: %s\n", [scriptPath UTF8String]);
            return 1;
        }

        [[NSProcessInfo processInfo] setProcessName:@"BeReal Image Downloader"];
        unsetenv("PYTHONHOME");
        setenv("PYTHONNOUSERSITE", "1", 1);
        setenv("__PYVENV_LAUNCHER__", [venvPython UTF8String], 1);
        prependEnvPath("DYLD_FALLBACK_LIBRARY_PATH", pillowDylibs);

        const char *home = getenv("HOME");
        if (home != NULL && home[0] != '\0') {
            chdir(home);
        }

        PyStatus status;
        PyConfig config;
        PyConfig_InitPythonConfig(&config);

        wchar_t *venvPythonWide = Py_DecodeLocale([venvPython fileSystemRepresentation], NULL);
        wchar_t *scriptWide = Py_DecodeLocale([scriptPath fileSystemRepresentation], NULL);
        if (venvPythonWide == NULL || scriptWide == NULL) {
            fprintf(stderr, "Failed to decode launcher paths for Python runtime\n");
            PyMem_RawFree(venvPythonWide);
            PyMem_RawFree(scriptWide);
            return 1;
        }

        status = PyConfig_SetString(&config, &config.program_name, venvPythonWide);
        if (PyStatus_Exception(status)) {
            goto python_fail;
        }
        status = PyConfig_SetString(&config, &config.executable, venvPythonWide);
        if (PyStatus_Exception(status)) {
            goto python_fail;
        }
        status = PyConfig_SetString(&config, &config.run_filename, scriptWide);
        if (PyStatus_Exception(status)) {
            goto python_fail;
        }

        config.parse_argv = 0;
        config.install_signal_handlers = 1;

        status = Py_InitializeFromConfig(&config);
        if (PyStatus_Exception(status)) {
            goto python_fail;
        }

        PyConfig_Clear(&config);
        PyMem_RawFree(venvPythonWide);
        PyMem_RawFree(scriptWide);
        return Py_RunMain();

python_fail:
        PyConfig_Clear(&config);
        PyMem_RawFree(venvPythonWide);
        PyMem_RawFree(scriptWide);
        Py_ExitStatusException(status);
        return 1;
    }
}
