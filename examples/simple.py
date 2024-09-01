from slowstore import Slowstore
from pydantic import BaseModel

class SampleModel(BaseModel):
    name: str
    age: int = 0

    def birthday(self):
        self.age += 1

# Create the store to save data under "mydata" directory
store = Slowstore[SampleModel](SampleModel, "mydata", key_selector=lambda store, model: model.name)

# This is how you create an object in the store, 
# Note that the parameters are the same as the SampleModel needs
dennis = store.create(name="denis", age=32)

# immediately after previous line is evaluated,
# you will have a json file (mydata/dennis.json) represening this object

dennis.name = "DENIS"
# here the name in the json will also change from "dennis" to "DENIS"
# also the associated change will be tracked so you can further inspect if needed.

dennis.birthday()
# will also trigger another change in the age field and it will be reflected in the json file. 
print(dennis.model)

