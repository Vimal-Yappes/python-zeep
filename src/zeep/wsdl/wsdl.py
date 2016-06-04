from __future__ import print_function

import operator
from copy import deepcopy
from collections import OrderedDict

import six
from lxml import etree
from lxml.etree import QName

from zeep.parser import absolute_location, load_external, parse_xml
from zeep.utils import findall_multiple_ns
from zeep.wsdl import definitions, http, soap
from zeep.xsd import Schema
from zeep.xsd.context import ParserContext

NSMAP = {
    'wsdl': 'http://schemas.xmlsoap.org/wsdl/',
}


class Document(object):
    """A WSDL Document exists out of one or more definitions.

    There is always one 'root' definition which should be passed as the
    location to the Document.  This definition can import other definitions.
    These imports are non-transitive, only the definitions defined in the
    imported document are available in the parent definition.  This Document is
    mostly just a simple interface to the root definition.

    After all definitions are loaded the definitions are resolved. This
    resolves references which were not yet available during the initial
    parsing phase.

    """

    def __init__(self, location, transport):
        """Initialize a WSDL document.

        The root definition properties are exposed as entry points.

        :param location: Location of this WSDL
        :type location: string
        :param transport: The transport object to be used
        :type transport: zeep.transports.Transport

        """
        self.location = location if not hasattr(location, 'read') else None
        self.transport = transport

        # Dict with all definition objects within this WSDL
        self._definitions = {}

        # Dict with internal schema objects, used for lxml.ImportResolver
        self._parser_context = ParserContext()

        document = self._load_content(location)

        root_definitions = Definition(self, document, self.location)
        root_definitions.resolve_imports()

        # Make the wsdl definitions public
        self.schema = root_definitions.schema
        self.messages = root_definitions.messages
        self.port_types = root_definitions.port_types
        self.bindings = root_definitions.bindings
        self.services = root_definitions.services

    def __repr__(self):
        return '<WSDL(location=%r)>' % self.location

    def dump(self):
        print('')
        print("Prefixes:")
        for prefix, namespace in self.schema._prefix_map.items():
            print(' ' * 4, '%s: %s' % (prefix, namespace))

        type_instances = self.schema.types
        print('')
        print("Global types:")
        for type_obj in sorted(type_instances, key=lambda k: six.text_type(k)):
            print(' ' * 4, six.text_type(type_obj))

        print('')
        for service in self.services.values():
            print(six.text_type(service))
            for port in service.ports.values():
                print(' ' * 4, six.text_type(port))
                print(' ' * 8, 'Operations:')

                operations = sorted(
                    port.binding._operations.values(),
                    key=operator.attrgetter('name'))

                for operation in operations:
                    print('%s%s' % (' ' * 12, six.text_type(operation)))
                print('')

    def _load_content(self, location):
        """Load the XML content from the given location and return an
        lxml.Element object.

        :param location: The URL of the document to load
        :type location: string

        """
        if hasattr(location, 'read'):
            return self._parse_content(location.read())
        return load_external(
            location, self.transport, self._parser_context, self.location)

    def _parse_content(self, content, base_url=None):
        """Parse the content as XML and return the document.

        :param content: content to parse as XML
        :param content: string, file
        :param base_url: base url for loading referenced documents
        :param base_url: string

        """
        return parse_xml(
            content, self.transport, self._parser_context, base_url)


class Definition(object):
    """The Definition represents one wsdl:definition within a Document."""

    def __init__(self, wsdl, doc, location):
        self.wsdl = wsdl
        self.location = location

        self.schema = None
        self.port_types = {}
        self.messages = {}
        self.bindings = {}
        self.services = OrderedDict()

        self.imports = {}
        self._resolved_imports = False

        self.target_namespace = doc.get('targetNamespace')
        self.wsdl._definitions[self.target_namespace] = self
        self.nsmap = doc.nsmap

        # Process the definitions
        self.parse_imports(doc)

        self.schema = self.parse_types(doc)
        self.messages = self.parse_messages(doc)
        self.port_types = self.parse_ports(doc)
        self.bindings = self.parse_binding(doc)
        self.services = self.parse_service(doc)

    def __repr__(self):
        return '<Definition(location=%r)>' % self.location

    def get(self, name, key):
        container = getattr(self, name)
        if key in container:
            return container[key]

        for definition in self.imports.values():
            container = getattr(definition, name)
            if key in container:
                return container[key]
        raise IndexError("No definition %r found" % name)

    def resolve_imports(self):
        """Resolve all root elements (types, messages, etc)."""

        # Simple guard to protect against cyclic imports
        if self._resolved_imports:
            return
        self._resolved_imports = True

        # Create a reference to an imported schema if the definition has no
        # schema of it's own.
        if self.schema is None:
            for definition in self.imports.values():
                if definition.schema and not definition.schema.is_empty:
                    self.schema = definition.schema
            else:
                self.schema = Schema(
                    None, self.wsdl.transport, self.location,
                    self.wsdl._parser_context, self.location)

        for definition in self.imports.values():
            definition.resolve_imports()

        for message in self.messages.values():
            message.resolve(self)

        for port_type in self.port_types.values():
            port_type.resolve(self)

        for binding in self.bindings.values():
            binding.resolve(self)

        for service in self.services.values():
            service.resolve(self)

    def parse_imports(self, doc):
        """Import other WSDL definitions in this document.

        Note that imports are non-transitive, so only import definitions
        which are defined in the imported document and ignore definitions
        imported in that document.

        This should handle recursive imports though:

            A -> B -> A
            A -> B -> C -> A

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        for import_node in doc.findall("wsdl:import", namespaces=NSMAP):
            location = import_node.get('location')
            namespace = import_node.get('namespace')

            if namespace in self.wsdl._definitions:
                self.imports[namespace] = self.wsdl._definitions[namespace]
            else:

                document = self.wsdl._load_content(location)
                location = absolute_location(location, self.location)
                if etree.QName(document.tag).localname == 'schema':
                    self.schema = Schema(
                        document, self.wsdl.transport, location,
                        self.wsdl._parser_context, location)
                else:
                    wsdl = Definition(self.wsdl, document, location)
                    self.imports[namespace] = wsdl

    def parse_types(self, doc):
        """Return a `types.Schema` instance.

        Note that a WSDL can contain multiple XSD schema's. The schemas can
        reference each other using xsd:import statements.

            <definitions .... >
                <types>
                    <xsd:schema .... />*
                </types>
            </definitions>

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        namespace_sets = [
            {'xsd': 'http://www.w3.org/2001/XMLSchema'},
            {'xsd': 'http://www.w3.org/1999/XMLSchema'},
        ]

        # Find xsd:schema elements (wsdl:types/xsd:schema)
        types = doc.find('wsdl:types', namespaces=NSMAP)
        if types is None or len(types) == 0:
            schema_nodes = []
        else:
            schema_nodes = findall_multiple_ns(
                types, 'xsd:schema', namespace_sets)

        if not schema_nodes:
            return None

        # FIXME: This fixes `test_parse_types_nsmap_issues`, lame solution...
        schema_nodes = [
            self.wsdl._parse_content(etree.tostring(schema_node), self.location)
            for schema_node in schema_nodes
        ]

        if len(schema_nodes) == 1:
            return Schema(
                schema_nodes[0], self.wsdl.transport, self.location,
                self.wsdl._parser_context, self.location)

        # A wsdl can contain multiple schema nodes. These can import each other
        # by simply referencing them by the namespace. To handle this in a way
        # that lxml schema can also handle it we create a new container schema
        # which imports the other schemas.  Since imports are non-transitive we
        # need to copy the schema imports the newyl created container schema.

        # Create namespace mapping (namespace -> internal location)
        schema_ns = {}
        for i, schema_node in enumerate(schema_nodes):
            ns = schema_node.get('targetNamespace')
            int_name = schema_ns[ns] = 'intschema:xsd%d' % i
            self.wsdl._parser_context.schema_nodes.add(schema_ns[ns], schema_node)
            self.wsdl._parser_context.schema_locations[int_name] = self.location

        # Only handle the import statements from the 2001 xsd's for now
        import_tag = '{http://www.w3.org/2001/XMLSchema}import'

        # Create a new schema node with xsd:import statements for all
        # schema's listed here.
        container = etree.Element('{http://www.w3.org/2001/XMLSchema}schema')
        for i, schema_node in enumerate(schema_nodes):

            # Create a new xsd:import element to import the schema
            import_node = etree.Element(import_tag)
            import_node.set('schemaLocation', 'intschema:xsd%d' % i)
            if schema_node.get('targetNamespace'):
                import_node.set('namespace', schema_node.get('targetNamespace'))
            container.append(import_node)

            # Add the namespace mapping created earlier here to the import
            # statements.
            for import_node in schema_node.findall(import_tag):
                location = import_node.get('schemaLocation')
                namespace = import_node.get('namespace')
                if not location:
                    import_node.set('schemaLocation', schema_ns[namespace])

                container.append(deepcopy(import_node))

        schema_node = container
        return Schema(
            schema_node, self.wsdl.transport, self.location,
            self.wsdl._parser_context, self.location)

    def parse_messages(self, doc):
        """
            <definitions .... >
                <message name="nmtoken"> *
                    <part name="nmtoken" element="qname"? type="qname"?/> *
                </message>
            </definitions>

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        result = {}
        for msg_node in doc.findall("wsdl:message", namespaces=NSMAP):
            msg = definitions.AbstractMessage.parse(self, msg_node)
            result[msg.name.text] = msg
        return result

    def parse_ports(self, doc):
        """Return dict with `PortType` instances as values

            <wsdl:definitions .... >
                <wsdl:portType name="nmtoken">
                    <wsdl:operation name="nmtoken" .... /> *
                </wsdl:portType>
            </wsdl:definitions>

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        result = {}
        for port_node in doc.findall('wsdl:portType', namespaces=NSMAP):
            port_type = definitions.PortType.parse(self, port_node)
            result[port_type.name.text] = port_type
        return result

    def parse_binding(self, doc):
        """Parse the binding elements and return a dict of bindings.

        Currently supported bindings are Soap 1.1, Soap 1.2., HTTP Get and
        HTTP Post. The detection of the type of bindings is done by the
        bindings themselves using the introspection of the xml nodes.

        XML Structure::

            <wsdl:definitions .... >
                <wsdl:binding name="nmtoken" type="qname"> *
                    <-- extensibility element (1) --> *
                    <wsdl:operation name="nmtoken"> *
                       <-- extensibility element (2) --> *
                       <wsdl:input name="nmtoken"? > ?
                           <-- extensibility element (3) -->
                       </wsdl:input>
                       <wsdl:output name="nmtoken"? > ?
                           <-- extensibility element (4) --> *
                       </wsdl:output>
                       <wsdl:fault name="nmtoken"> *
                           <-- extensibility element (5) --> *
                       </wsdl:fault>
                    </wsdl:operation>
                </wsdl:binding>
            </wsdl:definitions>

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        result = {}
        for binding_node in doc.findall('wsdl:binding', namespaces=NSMAP):
            # Detect the binding type
            if soap.Soap11Binding.match(binding_node):
                binding = soap.Soap11Binding.parse(self, binding_node)
            elif soap.Soap12Binding.match(binding_node):
                binding = soap.Soap12Binding.parse(self, binding_node)
            elif http.HttpGetBinding.match(binding_node):
                binding = http.HttpGetBinding.parse(self, binding_node)
            elif http.HttpPostBinding.match(binding_node):
                binding = http.HttpPostBinding.parse(self, binding_node)
            else:
                continue

            result[binding.name.text] = binding
        return result

    def parse_service(self, doc):
        """
            <wsdl:definitions .... >
                <wsdl:service .... > *
                    <wsdl:port name="nmtoken" binding="qname"> *
                       <-- extensibility element (1) -->
                    </wsdl:port>
                </wsdl:service>
            </wsdl:definitions>

        :param doc: The source document
        :type doc: lxml.etree._Element

        """
        result = OrderedDict()
        for service_node in doc.findall('wsdl:service', namespaces=NSMAP):
            service = definitions.Service.parse(self, service_node)
            result[service.name] = service
        return result
