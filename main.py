"""
Copyright 2014 Google Inc. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

This file serves two main purposes
- it serves up the main html page
- and it provides a simple set of apis to the javascript
"""

# If this is set to a valid API key string, then the Google
# backend will be enabled.
GOOGLE_API_KEY = None

import httplib2
import jinja2
import json
import logging
import os
import re
import socket
import webapp2

socket.setdefaulttimeout(60)
http = httplib2.Http(timeout=60)

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)),
    autoescape=True,
    extensions=['jinja2.ext.autoescape'])

# TODO: Dataset information should come from the list datasets api call
SUPPORTED_BACKENDS = {
  'NCBI' : {'name': 'NCBI',
            'url': 'http://trace.ncbi.nlm.nih.gov/Traces/gg/%s?%s',
            'datasets': {'SRP034507': 'SRP034507', 'SRP029392': 'SRP029392'}},
  'EBI' : {'name': 'EBI',
            'url': 'http://193.62.52.16/%s?%s',
            'datasets': {'All data': 'data'}},
  'LOCAL' : {'name': 'Local',
             'url': 'http://localhost:5000/%s?%s',
             'datasets': {'All': ''}},
}

if GOOGLE_API_KEY:
  # Google requires a valid API key
  SUPPORTED_BACKENDS['GOOGLE'] = {
    'name': 'Google',
    'url': 'https://www.googleapis.com/genomics/v1beta/%s?key='
           + GOOGLE_API_KEY + "&%s",
    'supportsNameFilter': True,
    'datasets': {'1000 Genomes': '376902546192',
                 'DREAM SMC Challenge': '337315832689',
                 'PGP': '383928317087',
                 'Simons Foundation' : '461916304629'}
  }


class ApiException(Exception):
  pass


# Request handlers
class BaseRequestHandler(webapp2.RequestHandler):
  def handle_exception(self, exception, debug_mode):
    if isinstance(exception, ApiException):
      # ApiExceptions are expected, and will return nice error
      # messages to the client
      self.response.write(exception.message)
      self.response.set_status(400)
    else:
      # All other exceptions are unexpected and should be logged
      logging.exception('Unexpected exception')
      self.response.write('Unexpected internal exception')
      self.response.set_status(500)

  def get_backend(self):
    backend = self.request.get('backend')
    if not backend:
      raise ApiException('Backend parameter must be set')
    return backend

  def supports_name_filter(self):
    return SUPPORTED_BACKENDS[self.get_backend()].has_key('supportsNameFilter')

  def get_base_api_url(self):
    return SUPPORTED_BACKENDS[self.get_backend()]['url']

  def get_content(self, path, method='POST', body=None, params=""):
    response, content = http.request(
      uri= self.get_base_api_url() % (path, params),
      method=method, body=json.dumps(body) if body else None,
      headers={'Content-Type': 'application/json; charset=UTF-8'})

    try:
      content = json.loads(content)
    except ValueError:
      logging.error("non-json api content %s" % content[:1000])
      raise ApiException('The API returned invalid JSON')

    if response.status >= 300:
      logging.error("error api response %s" % response)
      logging.error("error api content %s" % content)
      if 'error' in content:
        raise ApiException(content['error']['message'])
      else:
        raise ApiException('Something went wrong with the API call!')
    return content

  def write_content(self, path, method='POST', body=None):
    self.response.write(json.dumps(self.get_content(path, method, body)))


class ReadsetSearchHandler(BaseRequestHandler):

  def get(self):
    readset_id = self.request.get('readsetId')
    if not readset_id:
      dataset_id = self.request.get('datasetId')
      name = self.request.get('name')
      if dataset_id:
        body = {'datasetIds' : [dataset_id]}
      else:
        # This is needed for the local readstore
        body = {'datasetIds' : []}

      if self.supports_name_filter():
        body['name'] = name

      response = self.get_content("readsets/search", body=body,
                                  params="fields=readsets(id,name)")

      if not self.supports_name_filter() and name:
        name = name.lower()
        response['readsets'] = [r for r in response['readsets']
                                if name in r['name'].lower()]
      self.response.write(json.dumps(response))
      return

    # Single readset response
    self.write_content("readsets/%s" % readset_id, method='GET')


class ReadSearchHandler(BaseRequestHandler):

  def get(self):
    body = {
      'readsetIds': self.request.get('readsetIds').split(','),
      'sequenceName': self.request.get('sequenceName'),
      'sequenceStart': max(0, int(self.request.get('sequenceStart'))),
      'sequenceEnd': int(self.request.get('sequenceEnd')),
    }
    pageToken = self.request.get('pageToken')
    if pageToken:
      body['pageToken'] = pageToken
    self.write_content("reads/search", body=body)


class BaseSnpediaHandler(webapp2.RequestHandler):
  def getSnppediaPageContent(self, snp):
    uri = "http://bots.snpedia.com/api.php?action=query&prop=revisions&" \
          "format=json&rvprop=content&titles=%s" % snp
    response, content = http.request(uri=uri)

    page_id, page = json.loads(content)['query']['pages'].popitem()
    return page['revisions'][0]['*']

  def getContentValue(self, content, key):
    try:
      matcher = '%s=(.*)\n' % key
      return re.search(matcher, content, re.I).group(1)
    except (KeyError, AttributeError):
      return ''

  def complement(self, base):
    return {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G'}[base]


class SnpSearchHandler(BaseSnpediaHandler):

  def getSnpResponse(self, name, content):
    return {
      'name': name,
      'link': 'http://www.snpedia.com/index.php/%s' % name,
      'position': self.getContentValue(content, 'position'),
      'chr': self.getContentValue(content, 'chromosome')
    }

  def get(self):
    snp = self.request.get('snp')

    try:
      content = self.getSnppediaPageContent(snp)
      if snp[:2].lower() == 'rs':
        snps = [self.getSnpResponse(snp, content)]
      else:
        # Try a gene format
        snps = re.findall('\[\[(rs\d+?)\]\]', content, re.I)
        snps = [self.getSnpResponse(s, self.getSnppediaPageContent(s))
                for s in set(snps)]

    except (ValueError, KeyError, AttributeError):
      snps = []
    self.response.write(json.dumps({'snps' : snps}))


class AlleleSearchHandler(BaseSnpediaHandler):

  def getAlleleResponse(self, name, content):
    return {
      'name': name,
      'link': 'http://www.snpedia.com/index.php/%s' % name,
      'repute': self.getContentValue(content, 'repute'),
      'summary': self.getContentValue(content, 'summary') or 'Unknown',
      'magnitude': self.getContentValue(content, 'magnitude')
    }

  def get(self):
    snp = self.request.get('snp')
    a1 = self.request.get('a1')
    a2 = self.request.get('a2')

    a1c = self.complement(a1)
    a2c = self.complement(a2)
    possible_names = [(snp, a1, a2), (snp, a2, a1),
                      (snp, a1c, a2c), (snp, a2c, a1c)]
    for name in possible_names:
      try:
        page = "%s(%s;%s)" % name
        content = self.getSnppediaPageContent(page)
        self.response.write(json.dumps(self.getAlleleResponse(page, content)))
        return
      except (ValueError, KeyError, AttributeError):
        pass # Continue trying the next allele name

    self.response.write(json.dumps({}))


class MainHandler(webapp2.RequestHandler):

  def get(self):
    template = JINJA_ENVIRONMENT.get_template('main.html')
    self.response.write(template.render({
      'backends': SUPPORTED_BACKENDS,
    }))

web_app = webapp2.WSGIApplication(
    [
     ('/', MainHandler),
     ('/api/reads', ReadSearchHandler),
     ('/api/readsets', ReadsetSearchHandler),
     ('/api/snps', SnpSearchHandler),
     ('/api/alleles', AlleleSearchHandler),
    ],
    debug=True)
