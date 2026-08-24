'''Exceptions raised while parsing and evaluating corpus addresses.'''


class AddressError(Exception):
    '''Base class for every immediate address failure.'''


class SelectorError(AddressError):
    '''The supplied text is not a legal selector.'''


class EvaluationError(AddressError):
    '''A legal selector has no unambiguous denotation in a corpus.'''
