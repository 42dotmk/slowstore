import shutil

from pydantic import BaseModel

from slowstore import Store

# start clean so the example is repeatable
shutil.rmtree("mydata", ignore_errors=True)


class SampleModel(BaseModel):
    name: str
    age: int = 0

    def birthday(self):
        self.age += 1


# Create the store to save data under the "mydata" directory. `key_selector`
# derives the key each object is tracked/stored by (here, its name).
store = Store[SampleModel](SampleModel, "mydata", key_selector=lambda model: model.name)

# Add an object to the store. `set` returns a tracked proxy that behaves like the
# model but auto-syncs changes to disk.
dennis = store.set(SampleModel(name="denis", age=32))

# immediately after the previous line, there is a json file (mydata/denis.json)
# representing this object.

dennis.name = "DENIS"
# the name in the json also changes from "denis" to "DENIS", and the change is
# tracked so you can inspect it later.

dennis.birthday()
# triggers another change on the age field, also reflected in the json file.

print(dennis.model)
