#ifndef CONTROLLERS_PYTHON_H
#define CONTROLLERS_PYTHON_H

#include "jiminy/python/Utilities.h"


namespace jiminy
{
namespace python
{
    void exposeAbstractController(void);
    void exposeControllerFunctor(void);
}  // End of namespace python.
}  // End of namespace jiminy.

#endif  // CONTROLLERS_PYTHON_H
