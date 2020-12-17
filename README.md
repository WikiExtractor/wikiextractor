# WikiExtractor
[WikiExtractor.py](http://medialab.di.unipi.it/wiki/Wikipedia_Extractor) is a Python script that extracts and cleans text from a [Wikipedia database dump](https://dumps.wikimedia.org/).

The tool is written in Python and requires Python 3 but no additional library.
**Warning**: problems have been reported on Windows due to poor support for `StringIO` in the Python implementation on Windows.

For further information, see the [Wiki](https://github.com/attardi/wikiextractor/wiki).

# Wikipedia Cirrus Extractor

`cirrus-extractor.py` is a version of the script that performs extraction from a Wikipedia Cirrus dump.
Cirrus dumps contain text with already expanded templates.

Cirrus dumps are available at:
[cirrussearch](http://dumps.wikimedia.org/other/cirrussearch/).

# Details

WikiExtractor performs template expansion by preprocessing the whole dump and extracting template definitions.

In order to speed up processing:

- multiprocessing is used for dealing with articles in parallel
- a cache is kept of parsed templates (only useful for repeated extractions).

## Installation

The script may be invoked directly:

    python -m wikiextractor.WikiExtractor <Wikipedia dump file>

It can also be installed from `PyPi` by doing:

    pip install wikiextractor

or locally with:

    (sudo) python setup.py install

The installer also installs two scripts for direct invocation:

    wikiextractor  	(equivalent to python -m wikiextractor.WikiExtractor)
    extractPage		(to extract a single page from a dump)

## Usage

### Wikiextractor
The script is invoked with a Wikipedia dump file as an argument:

    python -m wikiextractor.WikiExtractor <Wikipedia dump file> [--templates <extracted template file>]

The option `--templates` extracts the templates to a local file, which can be reloaded to reduce the time to perform extraction.

The output is stored in several files of similar size in a given directory.
Each file will contains several documents in this [document format](https://github.com/attardi/wikiextractor/wiki/File-Format).

```
usage: wikiextractor [-h] [-o OUTPUT] [-b n[KMG]] [-c] [--json] [--html] [-l] [-ns ns1,ns2]
			 [--templates TEMPLATES] [--no-templates] [--html-safe HTML_SAFE] [--processes PROCESSES]
			 [-q] [--debug] [-a] [-v]
			 input

Wikipedia Extractor:
Extracts and cleans text from a Wikipedia database dump and stores output in a
number of files of similar size in a given directory.
Each file will contain several documents in the format:

	<doc id="" url="" title="">
	    ...
	    </doc>

If the program is invoked with the --json flag, then each file will                                            
contain several documents formatted as json ojects, one per line, with                                         
the following structure

	{"id": "", "revid": "", "url": "", "title": "", "text": "..."}

The program performs template expansion by preprocesssng the whole dump and
collecting template definitions.

positional arguments:
  input                 XML wiki dump file

optional arguments:
  -h, --help            show this help message and exit
  --processes PROCESSES
			    Number of processes to use (default 79)

Output:
  -o OUTPUT, --output OUTPUT
			    directory for extracted files (or '-' for dumping to stdout)
  -b n[KMG], --bytes n[KMG]
			    maximum bytes per output file (default 1M)
  -c, --compress        compress output files using bzip
  --json                write output in json format instead of the default <doc> format

Processing:
  --html                produce HTML output, subsumes --links
  -l, --links           preserve links
  -ns ns1,ns2, --namespaces ns1,ns2
			    accepted namespaces
  --templates TEMPLATES
			    use or create file containing templates
  --no-templates        Do not expand templates
  --html-safe HTML_SAFE
			    use to produce HTML safe output within <doc>...</doc>

Special:
  -q, --quiet           suppress reporting progress info
  --debug               print debug info
  -a, --article         analyze a file containing a single article (debug option)
  -v, --version         print program version
```

Saving templates to a file will speed up performing extraction the next time,
assuming template definitions have not changed.

Option `--no-templates` significantly speeds up the extractor, avoiding the cost
of expanding [MediaWiki templates](https://www.mediawiki.org/wiki/Help:Templates).

For further information, visit [the documentation](http://attardi.github.io/wikiextractor).

### Cirrus Extractor

~~~
usage: cirrus-extract.py [-h] [-o OUTPUT] [-b n[KMG]] [-c] [-ns ns1,ns2] [-q]
                         [-v]
                         input

Wikipedia Cirrus Extractor:
Extracts and cleans text from a Wikipedia Cirrus dump and stores output in a
number of files of similar size in a given directory.
Each file will contain several documents in the format:

	<doc id="" url="" title="" language="" revision="">
        ...
        </doc>

positional arguments:
  input                 Cirrus Json wiki dump file

optional arguments:
  -h, --help            show this help message and exit

Output:
  -o OUTPUT, --output OUTPUT
                        directory for extracted files (or '-' for dumping to
                        stdin)
  -b n[KMG], --bytes n[KMG]
                        maximum bytes per output file (default 1M)
  -c, --compress        compress output files using bzip

Processing:
  -ns ns1,ns2, --namespaces ns1,ns2
                        accepted namespaces

Special:
  -q, --quiet           suppress reporting progress info
  -v, --version         print program version
~~~

### extractPage
Extract a single page from a Wikipedia dump file.

~~~
usage: extractPage [-h] [--id ID] [--template] [-v] input

Wikipedia Page Extractor:
Extracts a single page from a Wikipedia dump file.

positional arguments:
  input          XML wiki dump file

optional arguments:
  -h, --help     show this help message and exit
  --id ID        article number
  --template     template number
  -v, --version  print program version
~~~

## License
The code is made available under the [GNU Affero General Public License v3.0](LICENSE). 

## Reference
If you find this code useful, please refer it in publications as:

~~~
@misc{Wikiextractor2015,
  author = {Giusepppe Attardi},
  title = {WikiExtractor},
  year = {2015},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/attardi/wikiextractor}}
}
~~~
