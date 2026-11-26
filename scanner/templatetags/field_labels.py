from django import template

from scanner.services.field_labels import field_label

register = template.Library()
register.filter("field_label", field_label)
