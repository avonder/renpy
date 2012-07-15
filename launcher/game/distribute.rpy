# This file contains code that manages the distribution of Ren'Py games 
# and Ren'Py proper.
#
# In this module, all files and paths are stored in unicode. Full paths
# might include windows path separators (\), but archive paths and names we
# deal with/match against use the unix separator (/).


init python in distribute:

    from store import config, persistent
    import store.project as project
    import store.interface as interface
    import store.archiver as archiver
    import store.updater as updater
    import store as store

    from change_icon import change_icons

    import sys
    import os
    import json
    import subprocess
    import hashlib
    import struct

    # Patterns that match files that should be given the executable bit.
    XBIT_PATTERNS = [
        "renpy.sh",
        "lib/**/*.so.*",
        "lib/**/*.so",
        "renpy.app/**/*.so.*",
        "renpy.app/**/*.so",
        "renpy.app/**/*.dylib",
        "renpy.app/Contents/MacOS/*",
        "lib/**/python",
        "lib/**/zsync",
        "lib/**/zsyncmake",
    ]
    
    import collections
    import os
    import io
    import re

    match_cache = { }
    
    def compile_match(pattern):
        """
        Compiles a pattern for use with match.
        """
        
        regexp = ""
        
        while pattern:
            if pattern.startswith("**"):
                regexp += r'.*'
                pattern = pattern[2:]
            elif pattern[0] == "*":
                regexp += r'[^/]*'
                pattern = pattern[1:]
            elif pattern[0] == '[':
                regexp += r'['
                pattern = pattern[1:]
                
                while pattern and pattern[0] != ']':
                    regexp += pattern[0]
                    pattern = pattern[1:]
                    
                pattern = pattern[1:]
                regexp += ']'
                
            else:
                regexp += re.escape(pattern[0])
                pattern = pattern[1:]
                
        regexp += "$"
        
        return re.compile(regexp, re.I)

    def match(s, pattern):
        """
        Matches a glob-style pattern against s. Returns True if it matches,
        and False otherwise.

        ** matches every character.
        * matches every character but /.
        [abc] matches a, b, or c.

        Things are matched case-insensitively.
        """
        
        regexp = match_cache.get(pattern, None)
        if regexp is None:
            regexp = compile_match(pattern)
            match_cache[pattern] = regexp
            
        if regexp.match(s):
            return True
    
        if regexp.match("/" + s):
            return True

        return False

    class File(object):
        """
        Represents a file that we can distribute.
        
        self.name
            The name of the file as it will be stored in the archives.
        
        self.path
            The path to the file on disk. None if it won't be stored
            on disk.
        
        self.directory
            True if this is a directory.
        
        self.executable
            True if this is an executable that should be distributed
            with the xbit set.
        """
        
        def __init__(self, name, path, directory, executable):
            self.name = name
            self.path = path
            self.directory = directory
            self.executable = executable
            
        def __repr__(self):
            if self.directory:
                extra = "dir"
            elif self.executable:
                extra = "x-bit"
            else:
                extra = ""
            
            return "<File {!r} {!r} {}>".format(self.name, self.path, extra)
    
        def copy(self):
            return File(self.name, self.path, self.directory, self.executable)

    
    class FileList(list):
        """
        This represents a list of files that we know about.
        """
    
        def sort(self):
            list.sort(self, key=lambda a : a.name)
        
        def copy(self):
            """
            Makes a deep copy of this file list.
            """
            
            rv = FileList()
            
            for i in self:
                rv.append(i.copy())
                
            return rv
        
        @staticmethod
        def merge(l):
            """
            Merges a list of file lists into a single file list with no
            duplicate entries.
            """
            
            rv = FileList()
            
            seen = set()
            
            for fl in l:
                for f in fl:
                    if f.name in seen:
                        continue
            
                    rv.append(f)
                    seen.add(f.name)
                    
            return rv

        def prepend_directory(self, directory):
            """
            Modifies this file list such that every file in it has `directory`
            prepended.
            """
            
            for i in self:
                i.name = directory + "/" + i.name
            
            self.insert(0, File(directory, None, True, False))
    
    
        def mac_transform(self, app, documentation):
            """
            Creates a new file list that has the mac transform applied to it.
            
            The mac transform places all files that aren't already in <app> in
            <app>/Contents/Resources/autorun. If it matches one of the documentation
            patterns, then it appears both inside and outside of the app.
            """
            
            rv = FileList()
            
            for f in self:
            
                # Already in the app.
                if f.name == app or f.name.startswith(app + "/"):
                    rv.append(f)
                    continue
                    
                # If it's documentation, keep the file. (But also make 
                # a copy.)
                for pattern in documentation:
                    if match(f.name, pattern):
                        rv.append(f)
                        
                # Make a copy.
                f = f.copy()
                
                f.name = app + "/Contents/Resources/autorun/" + f.name
                rv.append(f)
                
            rv.append(File(app + "/Contents/Resources/autorun", None, True, False))
            rv.sort()
    
            return rv


    class Distributor(object):
        """
        This manages the process of building distributions.
        """
        
        def __init__(self, project, destination=None, reporter=None, packages=None, build_update=True):
            """
            Distributes `project`.
            
            `destination`
                The destination in which the distribution will be placed. If None,
                uses a default location.
            
            `reporter`
                An object that's used to report status and progress to the user.
            
            `packages`
                If not None, a list of packages to distributed. If None, all
                packages are distributed.
            
            `build_update`
                Will updates be built?
            """
    
    
            # Safety - prevents us frome releasing a launcher that won't update.
            if store.UPDATE_SIMULATE:
                raise Exception("Cannot build distributions when UPDATE_SIMULATE is True.")

            # The project we want to distribute.
            self.project = project

            # Logfile.
            self.log = open(self.temp_filename("distribute.txt"), "w")

            # Start by scanning the project, to get the data and build
            # dictionaries.
            data = project.data
            
            project.update_dump(force=True, gui=False)
            if project.dump.get("error", False):
                raise Exception("Could not get build data from the project. Please ensure the project runs.")

            build = project.dump['build']

            # Map from file list name to file list.
            self.file_lists = collections.defaultdict(FileList)

            self.base_name = build['directory_name']
            self.executable_name = build['executable_name']
            self.pretty_version = build['version']
    
            # The destination directory.
            if destination is None:
                parent = os.path.dirname(project.path)
                self.destination = os.path.join(parent, self.base_name + "-dists")
                try:
                    os.makedirs(self.destination)
                except:
                    pass
            else:
                self.destination = destination

            # Status reporter.
            self.reporter = reporter

            self.include_update = build['include_update']
            self.build_update = self.include_update and build_update

            # The various executables, which change names based on self.executable_name.
            self.app = self.executable_name + ".app"
            self.exe = self.executable_name + ".exe"
            self.sh = self.executable_name + ".sh"
            self.py = self.executable_name + ".py"

            self.documentation_patterns = build['documentation_patterns']

            build_packages = [ ]
            
            for i in build['packages']:
                name = i['name']
                
                if (packages is None) or (name in packages):
                    build_packages.append(i)
                    
            if not build_packages:
                self.reporter.info(_("Nothing to do."), pause=True)
                return

            # add the game.
            self.reporter.info(_("Scanning project files..."))

            self.scan_and_classify(project.path, build["base_patterns"])

            self.archive_files(build["archives"])

            # Add Ren'Py.
            self.reporter.info(_("Scanning Ren'Py files..."))
            self.scan_and_classify(config.renpy_base, build["renpy_patterns"])

            # Add generated/special files.
            if not build['renpy']:
                self.add_renpy_files()
                self.add_mac_files()
                self.add_windows_files()

            # Assign the x-bit as necessary.
            self.mark_executable()

            # Rename the executable-like files.
            if not build['renpy']:
                self.rename()

            # The time of the update version.
            self.update_version = int(time.time())

            for p in build_packages:
                
                for f in p["formats"]:
                    self.make_package(
                        p["name"],
                        f,
                        p["file_lists"])
                
                if self.build_update and p["update"]:
                    self.make_package(
                        p["name"],
                        "update",
                        p["file_lists"])
            
            
            if self.build_update:
                self.finish_updates(build_packages)

            self.log.close()


        def scan_and_classify(self, directory, patterns):
            """
            Walks through the `directory`, finds files and directories that
            match the pattern, and assds them to the appropriate file list. 
            
            `patterns`
                A list of pattern, file_list tuples. The pattern is a string
                that is matched using match. File_list is either
                a space-separated list of file lists to add the file to, 
                or None to ignore it.
            
                Directories are matched with a trailing /, but added to the 
                file list with the trailing / removed.
            """
            
            def walk(name, path):
                is_dir = os.path.isdir(path)

                if is_dir:
                    match_name = name + "/"
                else:
                    match_name = name

                for pattern, file_list in patterns:
                    if match(match_name, pattern):
                        break
                else:
                    print >> self.log, match_name.encode("utf-8"), "doesn't match anything."

                    pattern = None
                    file_list = None

                print >> self.log, match_name.encode("utf-8"), "matches", pattern, "(" + str(file_list) + ")."

                if file_list is None:
                    return

                for fl in file_list:
                    f = File(name, path, is_dir, False)                
                    self.file_lists[fl].append(f)
                        
                if is_dir:

                    for fn in os.listdir(path):
                        walk(
                            name + "/" + fn,
                            os.path.join(path, fn),
                            )
                        
            for fn in os.listdir(directory):
                walk(fn, os.path.join(directory, fn))

        def temp_filename(self, name):
            self.project.make_tmp()
            return os.path.join(self.project.tmp, name)

        def add_file(self, file_list, name, path):
            """
            Adds a file to the file lists.
            
            `file_list`
                A space-separated list of file list names.
            
            `name`
                The name of the file to be added.
            
            `path`
                The path to that file on disk.
            """
        
            if not os.path.exists(path):
                raise Exception("{} does not exist.".format(path))
        
            if isinstance(file_list, basestring):
                file_list = file_list.split()
        
            f = File(name, path, False, False)
        
            for fl in file_list:
                self.file_lists[fl].append(f)

        def archive_files(self, archives):
            """
            Add files to archives.
            """
            
            for arcname, file_list in archives:
                
                if not self.file_lists[arcname]:
                    continue
                        
                arcfn = arcname + ".rpa"
                arcpath = self.temp_filename(arcfn)
                    
                af = archiver.Archive(arcpath)
                    
                fll = len(self.file_lists[arcname])
                 
                for i, entry in enumerate(self.file_lists[arcname]):

                    if entry.directory:
                        continue

                    self.reporter.progress(_("Archiving files..."), i, fll)

                    name = "/".join(entry.name.split("/")[1:])
                    af.add(name, entry.path)
                    
                self.reporter.progress_done()
                    
                af.close()
                
                self.add_file(file_list, "game/" + arcfn, arcpath)

        def add_renpy_files(self):
            """
            Add Ren'Py-generic files to the project.
            """
                
            self.add_file("all", "game/script_version.rpy", os.path.join(config.gamedir, "script_version.rpy"))
            self.add_file("all", "game/script_version.rpyc", os.path.join(config.gamedir, "script_version.rpyc"))
            self.add_file("all", "renpy/LICENSE.txt", os.path.join(config.renpy_base, "LICENSE.txt"))
                
        def add_mac_files(self):
            """
            Add mac-specific files to the distro.
            """
            
            # Rename the executable.
            self.add_file("mac", "renpy.app/Contents/MacOS/" + self.executable_name, os.path.join(config.renpy_base, "renpy.app/Contents/MacOS/Ren'Py Launcher"))
            
            # Update the plist file.
            quoted_name = self.executable_name.replace("&", "&amp;").replace("<", "&lt;")
            fn = self.temp_filename("Info.plist")
            
            with io.open(os.path.join(config.renpy_base, "renpy.app/Contents/Info.plist"), "r", encoding="utf-8") as old:
                data = old.read()
                
            data = data.replace("Ren'Py Launcher", quoted_name)
                
            with io.open(fn, "w", encoding="utf-8") as new:
                new.write(data)
                
            self.add_file("mac", "renpy.app/Contents/Info.plist", fn)

            # Update the launcher script.            
            quoted_name = self.executable_name.replace("\"", "\\\"")
            fn = self.temp_filename("launcher.py")
            
            with io.open(os.path.join(config.renpy_base, "renpy.app/Contents/Resources/launcher.py"), "r", encoding="utf-8") as old:
                data = old.read()
                
            data = data.replace("Ren'Py Launcher", quoted_name)
                
            with io.open(fn, "w", encoding="utf-8") as new:
                new.write(data)
                
            self.add_file("mac", "renpy.app/Contents/Resources/launcher.py", fn)

            # Icon file.
            custom_fn = os.path.join(self.project.path, "icon.icns")
            default_fn = os.path.join(config.renpy_base, "renpy.app/Contents/Resources/launcher.icns")
            
            if os.path.exists(custom_fn):
                self.add_file("mac", "renpy.app/Contents/Resources/launcher.icns", custom_fn)
            else:
                self.add_file("mac", "renpy.app/Contents/Resources/launcher.icns", default_fn)

        def add_windows_files(self):
            """
            Adds windows-specific files.
            """
            
            icon_fn = os.path.join(self.project.path, "icon.ico")
            old_exe_fn = os.path.join(config.renpy_base, "renpy.exe")
            
            if os.path.exists(icon_fn):
                exe_fn = self.temp_filename("renpy.exe")

                with open(exe_fn, "wb") as f:                    
                    f.write(change_icons(old_exe_fn, icon_fn))

            else:
                exe_fn = old_exe_fn
            
            self.add_file("windows", "renpy.exe", exe_fn) 

        def mark_executable(self):
            """
            Marks files as executable.
            """
            
            for l in self.file_lists.values():
                for f in l:
                    for pat in XBIT_PATTERNS:                        
                        if match(f.name, pat):    
                            f.executable = True

        def rename(self):
            """
            Rename files in all lists to match the executable names.
            """
            
            def rename_one(fn):
                parts = fn.split('/')
                p = parts[0]
                
                if p == "renpy.app":
                    p = self.app
                elif p == "renpy.exe":
                    p = self.exe
                elif p == "renpy.sh":
                    p = self.sh
                elif p == "renpy.py":
                    p = self.py
                    
                parts[0] = p
                return "/".join(parts)
            
            for l in self.file_lists.values():
                for f in l:
                    f.name = rename_one(f.name)

        def make_package(self, variant, format, file_lists, mac_transform=False):
            """
            Creates a package file in the projects directory. 

            `variant`
                The name of the variant to package. This is appended to the base name to become
                part of the file and directory names.

            `format`
                The format things will be packaged in. This should be one of "zip", "tar.bz2", or 
                "update". 
            
            `file_lists`
                A string containing a space-separated list of file_lists to include in this 
                package.
                        
            `mac_transform`
                True if we should apply the mac transform to the filenames before including 
                them.
            """
            filename = self.base_name + "-" + variant
            path = os.path.join(self.destination, filename)
            
            fl = FileList.merge([ self.file_lists[i] for i in file_lists ])
            fl = fl.copy()
            fl.sort()

            # Write the update information.
            update_files = [ ]
            update_xbit = [ ]
            update_directories = [ ]

            for i in fl:
                if not i.directory:
                    update_files.append(i.name)
                else:
                    update_directories.append(i.name)
                    
                if i.executable:
                    update_xbit.append(i.name)
                    
            update = { variant : { "version" : self.update_version, "pretty_version" : self.pretty_version, "files" : update_files, "directories" : update_directories, "xbit" : update_xbit } }

            if self.include_update:
                update_fn = os.path.join(self.destination, filename + ".update.json")

                with open(update_fn, "wb") as f:
                    json.dump(update, f)

                fl.append(File("update", None, True, False))
                fl.append(File("update/current.json", update_fn, False, False))

            # The mac transform.
            if format == "app-zip":
                fl = fl.mac_transform(self.app, self.documentation_patterns)
            
            # If we're not an update file, prepend the directory.
            if format != "update":
                fl.prepend_directory(filename)
            
            if format == "tar.bz2":
                path += ".tar.bz2"
                pkg = TarPackage(path, "w:bz2")
            elif format == "update":
                path += ".update"
                pkg = TarPackage(path, "w", notime=True)
            elif format == "zip" or format == "app-zip":
                path += ".zip"
                pkg = ZipPackage(path)
                
            for i, f in enumerate(fl):
                self.reporter.progress(_("Writing the [variant] [format] package."), i, len(fl), variant=variant, format=format)
                        
                if f.directory:
                    pkg.add_directory(f.name, f.path)
                else:
                    pkg.add_file(f.name, f.path, f.executable)

            self.reporter.progress_done()
            pkg.close()

            if format == "update":
                # Build the zsync file.
                # TODO: This should use an included copy of zsyncmake.
                
                self.reporter.info(_("Making the [variant] update zsync file."), variant=variant)
                
                subprocess.check_call([ 
                    updater.zsync_path("zsyncmake"), 
                    "-z", renpy.fsencode(path), 
                    "-u", filename + ".update.gz", 
                    "-o", renpy.fsencode(os.path.join(self.destination, filename + ".zsync")) ])

                # Build the sums file. This is a file with an adler32 hash of each 64k block
                # of the zsync file. It's used to help us determine how much of the file is 
                # downloaded.
                with open(path, "rb") as src:
                    with open(renpy.fsencode(os.path.join(self.destination, filename + ".sums")), "wb") as sums:
                        while True:
                            data = src.read(65536)
                            
                            if not data:
                                break
                                
                            sums.write(struct.pack("I", zlib.adler32(data) & 0xffffffff))
                            
                            



        def finish_updates(self, packages):
            """
            Indexes the updates, then removes the .update files.
            """

            if not self.build_update:
                return

            index = { }
            
            def add_variant(variant):
                fn = renpy.fsencode(os.path.join(self.destination, self.base_name + "-" + variant + ".update"))

                with open(fn, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                    
                index[variant] = { 
                    "version" : self.update_version, 
                    "pretty_version" : self.pretty_version, 
                    "digest" : digest, 
                    "zsync_url" : self.base_name + "-" + variant + ".zsync", 
                    "sums_url" : self.base_name + "-" + variant + ".sums", 
                    "json_url" : self.base_name + "-" + variant + ".update.json",
                    }
                
                os.unlink(fn)

            for p in packages:
                if p["update"]:
                    add_variant(p["name"])
                
            fn = renpy.fsencode(os.path.join(self.destination, "updates.json"))
            with open(fn, "wb") as f:
                json.dump(index, f)


        def dump(self):
            for k, v in sorted(self.file_lists.items()):
                print
                print k + ":"

                v.sort()

                for i in v:
                    print "   ", i.name, "xbit" if i.executable else ""

    class GuiReporter(object):
        """
        Displays progress using the gui.
        """
        
        def __init__(self):
            # The time at which we should next report progress.
            self.next_progress = 0

        def info(self, what, pause=False, **kwargs):
            if pause:
                interface.information(what, **kwargs)
            else:
                interface.processing(what, **kwargs)

        def progress(self, what, complete, total, **kwargs):
            
            if (complete > 0) and (time.time() < self.next_progress):
                return
                
            interface.processing(what, _("Processed {b}[complete]{/b} of {b}[total]{/b} files."), complete=complete, total=total, **kwargs)
            
            self.next_progress = time.time() + .05

        def progress_done(self):
            return


    class TextReporter(object):
        """
        Displays progress on the command line.
        """
        
        def info(self, what, pause=False, **kwargs):
            what = what.replace("[", "{")
            what = what.replace("]", "}")
            what = what.format(**kwargs)
            print what
            
        def progress(self, what, done, total, **kwargs):
            what = what.replace("[", "{")
            what = what.replace("]", "}")
            what = what.format(**kwargs)
            
            sys.stdout.write("\r{} - {} of {}".format(what, done + 1, total))
            sys.stdout.flush()
            
        def progress_done(self):
            sys.stdout.write("\n")


    def distribute_command():
        ap = renpy.arguments.ArgumentParser()
        ap.add_argument("--destination", "--dest", default=None, action="store", help="The directory where the packaged files should be placed.")
        ap.add_argument("--no-update", default=True, action="store_false", dest="build_update", help="Prevents updates from being built.")
        ap.add_argument("project", help="The path to the project directory.")
        ap.add_argument("--package", action="append", help="If given, a package to build. Defaults to building all packages.") 

        args = ap.parse_args()

        p = project.Project(args.project)
        
        if args.package:
            packages = args.package
        else:
            packages = None

        Distributor(p, destination=args.destination, reporter=TextReporter(), packages=packages, build_update=args.build_update)
        
        return False
        
    renpy.arguments.register_command("distribute", distribute_command)
            
label distribute:

    python hide:

        data = project.current.data
        d = distribute.Distributor(project.current, reporter=distribute.GuiReporter(), packages=data['packages'], build_update=data['build_update'])
        OpenDirectory(d.destination)()
        
        interface.info(_("All packages have been built.\n\nDue to the presence of permission information, unpacking and repacking the Linux and Macintosh distributions on Windows is not supported."))

    jump front_page
    

 
        
    
    
