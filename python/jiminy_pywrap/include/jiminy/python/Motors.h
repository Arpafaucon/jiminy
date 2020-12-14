#ifndef MOTORS_PYTHON_H
#define MOTORS_PYTHON_H

#include "jiminy/python/Utilities.h"


namespace jiminy
{
namespace python
{
    void exposeAbstractMotor(void);
    void exposeSimpleMotor(void);
}  // End of namespace python.
}  // End of namespace jiminy.

#endif  // MOTORS_PYTHON_H
