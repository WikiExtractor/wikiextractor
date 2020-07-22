# WikiExtractor
[WikiExtractor.py](http://medialab.di.unipi.it/wiki/Wikipedia_Extractor) is a Python script that extracts and cleans text from a [Wikipedia database dump](http://download.wikimedia.org/).

The tool is written in Python and requires Python 3 but no additional library.
**Warning**: problems have been reported on Windows due to poor support for `StringIO` in the Python implementation on Windows.

For further information, see the [project Home Page](http://medialab.di.unipi.it/wiki/Wikipedia_Extractor) or the [Wiki](https://github.com/attardi/wikiextractor/wiki).

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

    python -m wikiextractor.Wikiextractor

however it can also be installed from `PyPi` by doing:

    pip install wikiextractor

or locally with:

    (sudo) python setup.py install

## Usage

### Wikiextractor
The script is invoked with a Wikipedia dump file as an argument:

    python -m wikiextractor.Wikiextractor <Wikipedia dump file>

The output is stored in several files of similar size in a given directory.
Each file will contains several documents in this [document format](wiki/File-Format).

    usage: WikiExtractor.py [-h] [-o OUTPUT] [-b n[KMG]] [-c] [--json] [--html]
                            [-l] [-s] [--lists] [-ns ns1,ns2]
                            [--templates TEMPLATES] [--no-templates] [-r]
                            [--min_text_length MIN_TEXT_LENGTH]
                            [--filter_category path_of_categories_file]
                            [--filter_disambig_pages] [-it abbr,b,big]
                            [-de gallery,timeline,noinclude] [--keep_tables]
                            [--processes PROCESSES] [-q] [--debug] [-a] [-v]
                            [--log_file]
                            input

    Wikipedia Extractor:
    Extracts and cleans text from a Wikipedia database dump and stores output in a
    number of files of similar size in a given directory.
    Each file will contain several documents in the format:

        <doc id="" revid="" url="" title="">
            ...
            </doc>

    If the program is invoked with the --json flag, then each file will
    contain several documents formatted as json ojects, one per line, with
    the following structure

        {"id": "", "revid": "", "url":"", "title": "", "text": "..."}

    Template expansion requires preprocessing first the whole dump and
    collecting template definitions.

    positional arguments:
      input                 XML wiki dump file

    optional arguments:
      -h, --help            show this help message and exit
      --processes PROCESSES
                            Number of processes to use (default 1)

    Output:
      -o OUTPUT, --output OUTPUT
                            directory for extracted files (or '-' for dumping to
                            stdout)
      -b n[KMG], --bytes n[KMG]
                            maximum bytes per output file (default 1M)
      -c, --compress        compress output files using bzip
      --json                write output in json format instead of the default one

    Processing:
      --html                produce HTML output, subsumes --links
      -l, --links           preserve links
      -s, --sections        preserve sections
      --lists               preserve lists
      -ns ns1,ns2, --namespaces ns1,ns2
                            accepted namespaces in links
      --templates TEMPLATES
                            use or create file containing templates
      --no-templates        Do not expand templates
      -r, --revision        Include the document revision id (default=False)
      --min_text_length MIN_TEXT_LENGTH
                            Minimum expanded text length required to write
                            document (default=0)
      --filter_category path_of_categories_file
                            Include or exclude specific categories from the dataset. Specify the categories in
                            file 'path_of_categories_file'. Format:
                            One category one line, and if the line starts with:
                                1) #: Comments, ignored;
                                2) ^: the categories will be in excluding-categories
                                3) others: the categories will be in including-categories.
                            Priority:
                                1) If excluding-categories is not empty, and any category of a page exists in excluding-categories, the page will be excluded; else
                                2) If including-categories is not empty, and no category of a page exists in including-categories, the page will be excluded; else
                                3) the page will be included

      --filter_disambig_pages
                            Remove pages from output that contain disabmiguation
                            markup (default=False)
      -it abbr,b,big, --ignored_tags abbr,b,big
                            comma separated list of tags that will be dropped,
                            keeping their content
      -de gallery,timeline,noinclude, --discard_elements gallery,timeline,noinclude
                            comma separated list of elements that will be removed
                            from the article text
      --keep_tables         Preserve tables in the output article text
                            (default=False)

    Special:
      -q, --quiet           suppress reporting progress info
      --debug               print debug info
      -a, --article         analyze a file containing a single article (debug
                            option)
      -v, --version         print program version
      --log_file            specify a file to save the log information.


Saving templates to a file will speed up performing extraction the next time,
assuming template definitions have not changed.

Option --no-templates significantly speeds up the extractor, avoiding the cost
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
