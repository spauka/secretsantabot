"""
Config for Secret Santa Bot
"""
import configparser

class AttrDict(dict):
    def __init__(self, iterable=None, *, name=None, **kwargs):
        super().__init__(iterable, **kwargs)
        self.name = name

    def __getattr__(self, attr):
        if attr in self:
            if isinstance(self[attr], (dict, configparser.SectionProxy)):
                return AttrDict(self[attr], name=f"{self.name}.{attr}")
            return self[attr]
        raise AttributeError(f"Attribute {attr} not found in {self.name}.")

    def __setattr__(self, attr, val):
        if attr == "name":
            return super().__setattr__(attr, val)
        raise AttributeError(f"Cannot change configuration parameters at runtime. Please edit to config file instead.")

config = configparser.ConfigParser()
config.read("/home/spauka/secretsanta/secretsanta.cfg")
config = AttrDict(config, name="config")

def __getattr__(attr):
    return getattr(config, attr)
